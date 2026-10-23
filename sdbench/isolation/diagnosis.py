"""Run the released baseline using only the gatekeeper's public HTTP interface.

This process receives a presentation and requested observations, never a case file
or provider credential. Its output is a convenience log; the service's finalized
evidence is the only input accepted by the verifier.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class RemoteProtocolError(RuntimeError):
    """The isolated service rejected a request or returned invalid public data."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the bearer credential to a redirect destination.
        return None


def _integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise RemoteProtocolError(f"Invalid {name} in service response")
    return value


class RemoteTestRunner:
    """Structural implementation of SDBench's TestRunner protocol over HTTP."""

    def __init__(self, url: str, token: str, *, timeout: float = 180.0):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Service URL must be an HTTP(S) URL without credentials or query")
        if not token.strip() or any(c in token for c in "\r\n"):
            raise ValueError("Service token must be nonempty and on one line")
        if timeout <= 0:
            raise ValueError("HTTP timeout must be positive")
        self._url = url.rstrip("/")
        self._token = token.strip()
        self._timeout = timeout
        self.model_calls: list[dict[str, Any]] = []
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        session = self._request("GET", "/session")
        summary = session.get("case_summary")
        if not isinstance(summary, str) or not summary.strip():
            raise RemoteProtocolError("Service did not supply a case presentation")
        self.case_summary = summary
        self.diagnostic_agent = session.get("diagnostic_agent", "baseline")
        if self.diagnostic_agent != "baseline":
            raise RemoteProtocolError("Only the released baseline diagnostic agent is supported")
        self._max_turns = _integer(session.get("max_turns"), "max_turns")
        self._turns_left = _integer(session.get("turns_left"), "turns_left")
        if self._turns_left > self._max_turns:
            raise RemoteProtocolError("Remaining turns exceed the session limit")

    @property
    def max_turns(self) -> int:
        return self._max_turns

    @property
    def turns_left(self) -> int:
        return self._turns_left

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            self._url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                content = response.read(8 * 1024 * 1024 + 1)
        except HTTPError as exc:
            # Error bodies are not part of the public protocol and may contain
            # provider diagnostics. Do not copy them into the agent's output.
            status = exc.code
            exc.close()
            raise RemoteProtocolError(f"Service rejected {path} (HTTP {status})") from None
        except (URLError, TimeoutError, OSError):
            raise RemoteProtocolError(f"Service request failed for {path}") from None
        if len(content) > 8 * 1024 * 1024:
            raise RemoteProtocolError("Service response exceeds the size limit")
        try:
            value = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            raise RemoteProtocolError("Service response is not valid JSON") from None
        if not isinstance(value, dict):
            raise RemoteProtocolError("Service response must be a JSON object")
        return value

    def __call__(self, test_names: list[str]):
        from sdbench.gatekeeper import TestRunnerResponse

        if not isinstance(test_names, list) or not test_names or not all(
            isinstance(name, str) and name.strip() for name in test_names
        ):
            raise ValueError("Test requests must be a nonempty list of nonempty strings")
        data = self._request("POST", "/tests", {"test_names": test_names})
        results = data.get("test_results")
        limits = data.get("limit_messages")
        if not isinstance(results, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("test_name"), str)
            and isinstance(item.get("results"), str)
            for item in results
        ):
            raise RemoteProtocolError("Invalid test results in service response")
        if not isinstance(limits, list) or not all(isinstance(item, str) for item in limits):
            raise RemoteProtocolError("Invalid limit messages in service response")
        turns_left = _integer(data.get("turns_left"), "turns_left")
        if turns_left > self._turns_left:
            raise RemoteProtocolError("Service increased the remaining turn count")
        self._turns_left = turns_left
        # The released baseline consumes only names and observation text. Source
        # provenance is verifier-only metadata, and missing provenance must not
        # be represented as a fabricated is_synthetic=False value.
        public_results = [
            {"test_name": item["test_name"], "results": item["results"]} for item in results
        ]
        return TestRunnerResponse(
            test_names=[item["test_name"] for item in public_results],
            test_results=public_results,
            limit_messages=limits,
        )

    def chat_completion(self, messages: Iterable[dict[str, Any]], **kwargs: Any):
        from openai.types.chat import ChatCompletion

        if kwargs:
            raise ValueError("Diagnosis model and generation settings are fixed by the service")
        response = self._request("POST", "/model", {"messages": list(messages)})
        try:
            completion = ChatCompletion.model_validate(response)
        except ValueError:
            raise RemoteProtocolError("Service returned an invalid chat completion") from None
        if not completion.choices:
            raise RemoteProtocolError("Service returned a chat completion without choices")
        # These fields already crossed the public model boundary. Keep only
        # model identity and token counts, never provider extras or hidden text.
        self.model_calls.append({
            "model": completion.model,
            "usage": None if completion.usage is None else completion.usage.model_dump(
                include={
                    "prompt_tokens": True, "completion_tokens": True, "total_tokens": True,
                    "prompt_tokens_details": {"cached_tokens"},
                    "completion_tokens_details": {"reasoning_tokens"},
                },
                exclude_none=True,
            ),
        })
        return completion

    def submit_diagnosis(self, diagnosis: str) -> dict[str, Any]:
        if not isinstance(diagnosis, str) or not diagnosis.strip():
            raise ValueError("Final diagnosis must be a nonempty string")
        response = self._request("POST", "/diagnosis", {"diagnosis": diagnosis})
        if response.get("accepted") is not True:
            raise RemoteProtocolError("Service did not acknowledge the final diagnosis")
        return response


def run_diagnosis(url: str, token: str, *, timeout: float = 180.0) -> dict[str, Any]:
    """Execute the released baseline through the isolated service."""
    from sdbench.agents import baseline
    from sdbench.config import AgentConfig

    runner = RemoteTestRunner(url, token, timeout=timeout)
    collector: dict[str, Any] = {}
    config = AgentConfig(
        debug=False,
        clinician_cost=0,
        max_turns=runner.max_turns,
        max_questions_per_turn=None,
        max_tests_per_turn=None,
        agent_extra_args={},
    )
    diagnosis = baseline.run(
        case_summary=runner.case_summary,
        test_runner=runner,
        agent_llm=runner.chat_completion,
        collector=collector,
        config=config,
    )
    submission = runner.submit_diagnosis(diagnosis)
    return {
        "status": "completed",
        "diagnostic_agent": runner.diagnostic_agent,
        "diagnosis": diagnosis,
        "turns_left": runner.turns_left,
        "agent_messages": collector.get("agent_messages", []),
        "model_calls": runner.model_calls,
        "submission": submission,
        "authoritative": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    try:
        result = run_diagnosis(args.url, args.token_file.read_text().strip(), timeout=args.timeout)
        exit_code = 0
    except Exception as exc:
        # Do not serialize provider errors or credentials into public logs.
        result = {"status": "failed", "error_type": type(exc).__name__, "authoritative": False}
        exit_code = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
