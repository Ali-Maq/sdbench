"""Run the native Harbor CLI with temporary, refreshed Azure CLI credentials.

This is credential plumbing only. Harbor owns scheduling, containers, logs,
verification, retries, and resume. No Azure credentials enter generated tasks.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time


def refresh_token(directory: Path, tenant: str) -> float:
    """Capture Azure CLI output privately; atomically replace the mounted file."""
    try:
        result = subprocess.run(
            ["az", "account", "get-access-token", "--tenant", tenant,
             "--resource", "https://cognitiveservices.azure.com/", "--output", "json"],
            capture_output=True, text=True, check=True, timeout=60,
        )
        value = json.loads(result.stdout)
        token, expires = value["accessToken"], float(value["expires_on"])
        if (not isinstance(token, str) or not token or any(char.isspace() for char in token)
                or not math.isfinite(expires) or expires <= time.time() + 120):
            raise ValueError("Invalid or nearly expired token")
        # Do not retain tenant/account metadata, a login cache, or CLI stderr.
        destination = directory / "token.json"
        temporary = directory / ".token.next"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"accessToken": token, "expires_on": expires}, stream)
        os.replace(temporary, destination)
        return expires
    except Exception as error:
        raise RuntimeError(f"Azure CLI token acquisition failed ({type(error).__name__}); check az login") from None


def run(config: Path, tenant: str) -> int:
    harbor = shutil.which("harbor")
    if harbor is None:
        raise ValueError("Run this command in the environment containing Harbor 0.24.0")
    if not config.is_file():
        raise ValueError("Harbor job configuration does not exist")
    with tempfile.TemporaryDirectory(prefix="sdbench-azure-") as temporary:
        directory = Path(temporary)
        os.chmod(directory, 0o700)
        expires = refresh_token(directory, tenant)
        environment = os.environ.copy()
        environment["SDBENCH_AZURE_TOKEN_DIR"] = str(directory)
        process = subprocess.Popen([harbor, "run", "--config", str(config.resolve())], env=environment)
        try:
            while True:
                try:
                    return process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if time.time() >= expires - 300:
                        expires = refresh_token(directory, tenant)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--tenant", required=True)
    args = parser.parse_args()
    try:
        raise SystemExit(run(args.config, args.tenant))
    except (ValueError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
