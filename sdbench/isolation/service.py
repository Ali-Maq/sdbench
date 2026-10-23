"""Per-trial clinical request service and role-bound diagnosis model proxy.

This trusted service owns the case, gatekeeper state, and authoritative evidence.
It exposes no case-selection, filesystem, admin, or log-export route to the agent.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from sdbench.gatekeeper import BatchTestRunner
from sdbench.isolation.common import append_jsonl, canonical_hash, sdbench_config
from sdbench.isolation.providers import build_backend

MAX_BODY_BYTES = 1_000_000


class RequestRejected(ValueError):
    pass


class TrialService:
    def __init__(self, config: dict[str, Any], case: dict[str, Any], output_dir: str | Path):
        self.config = config
        self.case = case
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (self.output_dir / "events.jsonl").exists():
            raise ValueError("Refusing to reuse an existing encounter; create a fresh trial directory")
        if canonical_hash(case) != config["case_hash"] or str(case["case_id"]) != str(config["case_id"]):
            raise ValueError("Case identity/hash does not match trial manifest")
        self.token = config.get("diagnosis_token", "")
        if not isinstance(self.token, str) or len(self.token) < 32:
            raise ValueError("A high-entropy, per-trial diagnosis token is required")
        self.collector: dict[str, Any] = {}
        self.runner = BatchTestRunner(
            sdbench_config(config), case, self.collector,
            llm=build_backend(config, "gatekeeper", self.output_dir / "usage.jsonl"),
        )
        self.diagnosis_model = build_backend(config, "diagnosis", self.output_dir / "usage.jsonl")
        self.sequence = 0
        self.closed = False
        self._event("session", case_summary=case["case_summary"], max_turns=self.runner.max_turns, turns_left=self.runner.turns_left)

    def _event(self, event: str, **payload: Any) -> None:
        self.sequence += 1
        append_jsonl(self.output_dir / "events.jsonl", {
            "sequence": self.sequence, "event": event, "trial_id": self.config["trial_id"],
            "case_hash": self.config["case_hash"], "protocol_hash": self.config["protocol_hash"],
            "config_hash": canonical_hash(self.config), "timestamp": time.time(), **payload,
        })

    def authenticate(self, authorization: str) -> bool:
        return hmac.compare_digest(authorization, f"Bearer {self.token}")

    def session(self) -> dict[str, Any]:
        return {"case_summary": self.case["case_summary"], "max_turns": self.runner.max_turns, "turns_left": self.runner.turns_left}

    def dispatch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if self.closed:
            raise RequestRejected("Encounter has ended")
        if path == "/tests":
            if set(body) != {"test_names"}:
                raise RequestRejected("Expected test_names only")
            names = body["test_names"]
            if not isinstance(names, list) or not 1 <= len(names) <= 100 or any(
                not isinstance(name, str) or not name.strip() or len(name) > 10_000 for name in names
            ):
                raise RequestRejected("Invalid test_names")
            if self.runner.turns_left <= 0:
                raise RequestRejected("No test turns remain")
            response = asdict(self.runner(names))
            response["turns_left"] = self.runner.turns_left
            self._event("tests", requested_test_names=names, **response)
            # Provenance is for the verifier/auditor, not an extra clinical hint.
            return {**response, "test_results": [
                {"test_name": result["test_name"], "results": result["results"]}
                for result in response["test_results"]
            ]}
        if path == "/model":
            if set(body) != {"messages"}:
                raise RequestRejected("Expected messages only; model and role cannot be overridden")
            messages = body["messages"]
            if not isinstance(messages, list) or not messages or len(messages) > 500:
                raise RequestRejected("Invalid messages")
            for message in messages:
                if not isinstance(message, dict) or set(message) != {"role", "content"} or message["role"] not in {"system", "user", "assistant"} or not isinstance(message["content"], str):
                    raise RequestRejected("Only text system/user/assistant messages are supported")
            return self.diagnosis_model(messages=messages).model_dump()
        if path == "/diagnosis":
            if set(body) != {"diagnosis"} or not isinstance(body["diagnosis"], str) or not 1 <= len(body["diagnosis"].strip()) <= 20_000:
                raise RequestRejected("Expected a nonempty diagnosis only")
            diagnosis = body["diagnosis"].strip()
            self._event("diagnosis", diagnosis=diagnosis)
            self._event("terminal", status="completed", diagnosis=diagnosis)
            self.closed = True
            return {"accepted": True}
        raise RequestRejected("Unknown endpoint")

    def fail(self, error: Exception) -> None:
        if not self.closed:
            self._event("terminal", status="failed", error_type=type(error).__name__)
            self.closed = True


def make_server(service: TrialService, host: str = "127.0.0.1", port: int = 0) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Never put request bodies, credentials or case data in public logs.
            pass

        def send_json(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def authorized(self) -> bool:
            if service.authenticate(self.headers.get("Authorization", "")):
                return True
            append_jsonl(service.output_dir / "security.jsonl", {"event": "unauthorized_request", "timestamp": time.time()})
            self.send_json(401, {"error": "Unauthorized"})
            return False

        def do_GET(self) -> None:
            # Health has no state, case data or model metadata.
            if self.path == "/health":
                self.send_json(200, {"ok": True})
            elif self.authorized():
                if self.path == "/session" and not service.closed:
                    self.send_json(200, service.session())
                else:
                    self.send_json(404, {"error": "Not found"})

        def do_POST(self) -> None:
            if not self.authorized():
                return
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise RequestRejected("Transfer encoding is unsupported")
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= MAX_BODY_BYTES:
                    raise RequestRejected("Invalid request length")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise RequestRejected("Expected JSON object")
                self.send_json(200, service.dispatch(self.path, body))
            except (RequestRejected, json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
                self.send_json(400, {"error": str(error) if isinstance(error, RequestRejected) else "Invalid request"})
            except Exception as error:
                service.fail(error)
                self.send_json(503, {"error": "Encounter failed; details retained in protected evidence"})

    class Server(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(150)
            return connection, address

    return Server((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    os.environ["SDBENCH_LOCAL_DIR"] = "disable"
    service = TrialService(json.loads(args.config.read_text()), json.loads(args.case.read_text()), args.output_dir)
    server = make_server(service, args.host, args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
