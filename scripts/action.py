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
    if baseline := os.environ.get("CHECK_BASELINE_REGISTRY", ""):
        args += ["--baseline-registry", baseline]
    if data := os.environ.get("CHECK_ADVICE_DATA", ""):
        args += ["--advice-data", data]
    for policy in os.environ.get("CHECK_POLICIES", "").splitlines():
        if policy.strip():
            args += ["--policy", policy.strip()]
    for layer in os.environ.get("CHECK_REQUIRE", "").split():
        args += ["--require", layer]
    if command:
        # The caller explicitly supplies a shell command in the run input.
        args += ["--", "bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", command]
    else:
        args += [os.environ.get("CHECK_FILE", "telemetry.json")]
    summary_file = directory / "summary.json"
    previous = summary_file.read_bytes() if summary_file.exists() else None
    code = subprocess.call(args)
    if (os.environ.get("GITHUB_STEP_SUMMARY") and summary_file.exists()
            and summary_file.read_bytes() != previous):
        summary = json.loads(summary_file.read_text())
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
            output.write("### OpenTelemetry contract check\n\n| Layer | Result |\n| --- | --- |\n")
            for name, layer in summary["layers"].items():
                output.write(f'| {name} | {layer["status"]} |\n')
            output.write(f'\nWorkload exit: {summary.get("workload_exit_code")}; gate exit: {code}.\n')
            output.write("\nPASS applies only to the documented scope of each layer. NOT_CHECKED means evidence is absent.\n")
    raise SystemExit(code)
