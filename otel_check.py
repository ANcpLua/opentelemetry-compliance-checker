"""Small CI adapter around the pinned Weaver Rust/Rego engine (stdlib only)."""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
METADATA = json.loads((ROOT / "metadata/specifications.json").read_text())


def status(as_json=False):
    if as_json:
        print(json.dumps(METADATA, indent=2))
        return 0
    for spec in METADATA["specifications"]:
        print(f'{spec["id"]:8} {spec["version"] or "—":8} {spec["status"]}')
    print("\nSignal    API                   SDK          Protocol")
    for row in METADATA["signals"]:
        print(f'{row["signal"]:9} {row["api"] or "—":21} '
              f'{row["sdk"] or "—":12} {row["protocol"]}')
    print("\nSnapshot: " + METADATA["observed_at"] + "; specification status, not SDK support.")
    return 0


def stop_process(process):
    """Reap both the process and its descendants on POSIX, also after parent exit."""
    if process is None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.kill()
    process.wait()


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return True
    except OSError:
        return False


def available_ports():
    # Keep both sockets reserved until both numbers are selected.
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        return first.getsockname()[1], second.getsockname()[1]


def run_workload(engine_command, command, args, log):
    if os.name != "posix":
        raise ValueError("Live run mode requires Linux or macOS; file checks also work on Windows.")
    grpc_port, admin_port = available_ports()
    # Loopback only. A command receives the collector endpoint through standard OTel env vars.
    engine_command += ["--input-source", "otlp", "--otlp-grpc-address", "127.0.0.1",
                       "--otlp-grpc-port", str(grpc_port), "--admin-port", str(admin_port),
                       "--inactivity-timeout", str(int(args.timeout) + 90)]
    engine = workload = None
    try:
        engine = subprocess.Popen(engine_command, stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + args.startup_timeout
        while not (port_open(grpc_port) and port_open(admin_port)):
            if engine.poll() is not None:
                raise RuntimeError("Weaver exited before its receivers were ready; see engine.log.")
            if time.monotonic() >= deadline:
                raise TimeoutError("Weaver receiver startup timed out; see engine.log.")
            time.sleep(0.05)
        if engine.poll() is not None:
            raise RuntimeError("Weaver exited before the workload started.")
        env = os.environ.copy()
        for suffix in ("", "_TRACES", "_METRICS", "_LOGS"):
            env["OTEL_EXPORTER_OTLP" + suffix + "_ENDPOINT"] = f"http://127.0.0.1:{grpc_port}"
            env["OTEL_EXPORTER_OTLP" + suffix + "_PROTOCOL"] = "grpc"
        workload = subprocess.Popen(command, env=env, start_new_session=True)
        deadline = time.monotonic() + args.timeout
        while workload.poll() is None:
            if engine.poll() is not None:
                raise RuntimeError("Weaver stopped while the workload was still running.")
            if time.monotonic() >= deadline:
                raise TimeoutError("Workload timed out.")
            time.sleep(0.05)
        workload_code = workload.returncode
        # The workload must flush/shut down its SDK before exiting.
        if engine.poll() is not None:
            raise RuntimeError("Weaver stopped before the workload could be finalized.")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(f"http://127.0.0.1:{admin_port}/stop", method="POST")
        with opener.open(request, timeout=10) as response:
            response.read()
        return engine.wait(timeout=30), workload_code
    finally:
        stop_process(workload)
        stop_process(engine)


def check(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    summary = {
        "state": "running", "baseline": METADATA["specifications"],
        "registry": args.registry or METADATA["engine"]["registry"],
        "weaver_version": METADATA["engine"]["version"],
        "fail_on": args.fail_on, "report_path": None,
        "engine_log": str(run_dir / "engine.log"),
    }
    summary_path = output / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    result = 2
    try:
        if args.mode == "check":
            source = Path(args.input).resolve()
            samples = json.loads(source.read_text())
            if not isinstance(samples, list) or not samples:
                raise ValueError("Input must be a non-empty Weaver sample JSON array. OTLP JSON is a different format.")
        binary = shutil.which(os.environ.get("WEAVER", "weaver"))
        if not binary:
            raise ValueError("Weaver is missing. Install the pinned version, or use the GitHub Action.")
        version = subprocess.run([binary, "--version"], text=True, capture_output=True,
                                 check=True, timeout=10).stdout.strip()
        if version != "weaver " + METADATA["engine"]["version"]:
            raise ValueError(f"Expected Weaver {METADATA['engine']['version']}; found {version!r}.")
        engine_command = [
            binary, "--config", str(ROOT / "checker.weaver.toml"),
            "registry", "live-check", "--registry", summary["registry"],
            "--v2", "--format", "json", "--no-stream",
            "--no-stats=false", "--fail-on", args.fail_on, "--quiet",
            "--diagnostic-format", "json", "--output", str(run_dir),
        ]
        workload_code = 0
        with (run_dir / "engine.log").open("w") as log:
            if args.mode == "check":
                engine_command += ["--input-source", str(source), "--input-format", "json"]
                engine = subprocess.Popen(engine_command, stdout=log, stderr=log,
                                          start_new_session=os.name == "posix")
                try:
                    engine_code = engine.wait(timeout=args.timeout)
                finally:
                    stop_process(engine)
            else:
                command = args.command
                if command and command[0] == "--":
                    command = command[1:]
                if not command:
                    raise ValueError("run needs a command after --.")
                engine_code, workload_code = run_workload(engine_command, command, args, log)
        summary.update(engine_exit_code=engine_code, workload_exit_code=workload_code)
        report_path = run_dir / "live_check.json"
        report = json.loads(report_path.read_text())
        stats = report["statistics"]
        entities = stats["total_entities"]
        if type(entities) is not int or entities <= 0:
            raise ValueError("No telemetry was observed. A check with zero samples cannot pass.")
        summary.update(report_path=str(report_path), entities=entities,
                       findings=stats["advice_level_counts"])
        result = 1 if engine_code or workload_code else 0
        summary["state"] = "failed" if result else "passed"
    except (OSError, ValueError, KeyError, TypeError, RuntimeError,
            subprocess.SubprocessError, TimeoutError) as error:
        summary.update(state="error", error=str(error))
        print(f"otel-check: {error}", file=sys.stderr)
    except KeyboardInterrupt:
        result = 130
        summary.update(state="cancelled", error="Interrupted.")
    finally:
        summary["exit_code"] = result
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(f'otel-check: {summary["state"]}; {summary.get("entities", 0)} entities; '
          f'findings={summary.get("findings", {})}; report={summary_path}')
    return result


def positive(value):
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def interrupted(*_):
    raise KeyboardInterrupt()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    stat = commands.add_parser("status", help="Show the five specification baselines and signal status.")
    stat.add_argument("--json", action="store_true")
    for name in ("check", "run"):
        sub = commands.add_parser(name)
        sub.add_argument("--registry", help="Optional custom registry; default is the pinned upstream commit.")
        sub.add_argument("--fail-on", choices=("violation", "improvement", "information"), default="violation")
        sub.add_argument("--output", default="otel-report")
        sub.add_argument("--timeout", type=positive, default=300)
        sub.add_argument("--startup-timeout", type=positive, default=60)
        if name == "check":
            sub.add_argument("input", nargs="?", default="telemetry.json", help="Weaver sample JSON array.")
        else:
            sub.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.mode == "status":
        return status(args.json)
    # Reuse normal cleanup for CI cancellation.
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        return check(args)
    finally:
        signal.signal(signal.SIGTERM, previous)
