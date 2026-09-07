"""GitHub adapter: inputs are data, never interpolated into generated shell code."""
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
if sys.argv[1] == "pins":
    version = json.loads((root / "metadata/specifications.json").read_text())["engine"]["version"]
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"version={version}\n")
else:
    command = os.environ.get("CHECK_RUN", "")
    directory = Path(os.environ.get("CHECK_OUTPUT", "otel-report")).resolve()
    if "\n" in str(directory) or "\r" in str(directory):
        raise SystemExit("Output path must not contain newlines.")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"summary={directory / 'summary.json'}\n")
    args = [sys.executable, str(root / "otel-check"), "run" if command else "check",
            "--output", str(directory), "--timeout", os.environ.get("CHECK_TIMEOUT", "300"),
            "--fail-on", os.environ.get("CHECK_FAIL_ON", "violation")]
    if registry := os.environ.get("CHECK_REGISTRY", ""):
        args += ["--registry", registry]
    if command:
        # The caller explicitly supplies a shell command in the run input.
        args += ["--", "bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", command]
    else:
        args += [os.environ.get("CHECK_FILE", "telemetry.json")]
    raise SystemExit(subprocess.call(args))
