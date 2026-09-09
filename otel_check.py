"""Check OpenTelemetry samples and test workloads with Weaver."""

import argparse
import hashlib
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

from contract_report import LEVELS, SCOPES, finish, new_layers, read_compatibility, read_telemetry
from release_surface import cmd_packages, cmd_release, cmd_surface

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


def policy_bundle(args, run_dir, summary):
    """Snapshot bundled and user-supplied policies for this run."""
    directory = run_dir / "policies"
    directory.mkdir()
    paths = sorted((ROOT / "policies").rglob("*.rego"))
    for supplied in args.policy:
        path = Path(supplied).resolve()
        additions = sorted(path.rglob("*.rego")) if path.is_dir() else [path]
        if not additions or any(not p.is_file() or p.suffix != ".rego" for p in additions):
            raise ValueError(f"Policy must be a .rego file or a directory containing policies: {path}")
        paths.extend(additions)
    summary["policies"] = []
    for index, path in enumerate(paths):
        content = path.read_bytes()
        (directory / f"{index:04d}-{path.name}").write_bytes(content)
        summary["policies"].append({"path": str(path), "sha256": hashlib.sha256(content).hexdigest()})
    return directory


def compare_registry(binary, args, summary, run_dir):
    layer = summary["layers"]["compatibility"]
    if not args.baseline_registry:
        layer["reason"] = "No --baseline-registry supplied. Specification versions alone are not compatibility evidence."
        return
    log_path = run_dir / "compatibility.log"
    summary["compatibility_report_path"] = str(log_path)
    command = [binary, "--config", str(ROOT / "checker.weaver.toml"), "registry", "check",
               "--v2", "--registry", summary["registry"], "--baseline-registry", args.baseline_registry,
               "--policy", str(run_dir / "policies"), "--diagnostic-format", "json", "--quiet"]
    with log_path.open("w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=os.name == "posix")
        try:
            code = process.wait(timeout=args.timeout)
        finally:
            stop_process(process)
    summary["compatibility_exit_code"] = code
    raw = log_path.read_text().strip()
    read_compatibility(json.loads(raw) if raw else [], code, layer)


def check(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    summary = {
        "schema_version": 2, "state": "running", "baseline": METADATA["specifications"],
        "registry": args.registry or METADATA["engine"]["registry"],
        "weaver_version": METADATA["engine"]["version"],
        "fail_on": args.fail_on, "report_path": None,
        "engine_log": str(run_dir / "engine.log"),
        "baseline_registry": args.baseline_registry,
        "layers": new_layers(), "required_layers": sorted(set(args.require)),
        "mode": args.mode, "workload_exit_code": None,
    }
    summary_path = output / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    # Default to failure until all phases complete.
    operational_error, cancelled = True, False
    binary = None
    phase = "input"
    try:
        if args.mode == "check":
            source = Path(args.input).resolve()
            summary["input_path"] = str(source)
            samples = json.loads(source.read_text())
            if not isinstance(samples, list) or not samples:
                raise ValueError("Input must be a non-empty Weaver sample JSON array. OTLP JSON is a different format.")
        phase = "engine"
        binary = shutil.which(os.environ.get("WEAVER", "weaver"))
        if not binary:
            raise ValueError("Weaver is missing. Install the pinned version, or use the GitHub Action.")
        version = subprocess.run([binary, "--version"], text=True, capture_output=True,
                                 check=True, timeout=10).stdout.strip()
        if version != "weaver " + METADATA["engine"]["version"]:
            raise ValueError(f"Expected Weaver {METADATA['engine']['version']}; found {version!r}.")
        policies = policy_bundle(args, run_dir, summary)
        engine_command = [
            binary, "--config", str(ROOT / "checker.weaver.toml"),
            "registry", "live-check", "--registry", summary["registry"],
            "--v2", "--format", "json", "--no-stream",
            "--no-stats=false", "--fail-on", args.fail_on, "--quiet",
            "--diagnostic-format", "json", "--output", str(run_dir),
            "--advice-policies", str(policies),
        ]
        if args.advice_data:
            engine_command += ["--advice-data", args.advice_data]
        workload_code = None
        phase = "telemetry"
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
        if args.mode == "check" and engine_code and not report_path.exists():
            # Weaver decoding errors belong to the syntax layer.
            try:
                diagnostics = json.loads((run_dir / "engine.log").read_text())
            except (ValueError, OSError):
                diagnostics = []
            if isinstance(diagnostics, list):
                for diagnostic in diagnostics:
                    ingest = diagnostic.get("error", {}).get("IngestError")
                    if isinstance(ingest, dict) and isinstance(ingest.get("error"), str):
                        phase = "input"
                        raise ValueError(ingest["error"])
        report = json.loads(report_path.read_text())
        summary["report_path"] = str(report_path)
        entities, counts = read_telemetry(report, summary["layers"])
        summary.update(entities=entities, findings=counts,
                       registry_coverage=report["statistics"].get("registry_coverage"))
        if engine_code and not any(LEVELS[level] >= LEVELS[args.fail_on] and count
                                   for level, count in counts.items()):
            raise RuntimeError("Weaver failed without a finding at the selected threshold; see engine.log.")
        phase = "compatibility"
        compare_registry(binary, args, summary, run_dir)
        operational_error = False
    except Exception as error:
        # CLI boundary: retain unexpected failures as structured errors too.
        operational_error = True
        summary["error"] = str(error)
        summary["error_type"] = type(error).__name__
        if phase == "input":
            summary["layers"]["syntax"].update(status="FAIL", reason=str(error))
        elif phase == "compatibility":
            summary["layers"]["compatibility"].update(status="ERROR", reason=str(error))
        else:
            for name in ("syntax", "semantics", "behavior", "stability"):
                summary["layers"][name].update(status="ERROR", reason="Telemetry assessment did not complete: " + str(error))
        print(f"otel-check: {error}", file=sys.stderr)
    except KeyboardInterrupt:
        cancelled = True
        summary["error"] = "Interrupted."
    finally:
        result = finish(summary, operational_error, cancelled)
        summary["run_summary_path"] = str(run_dir / "summary.json")
        for name, layer in summary["layers"].items():
            layer["evidence_file"] = summary.get("compatibility_report_path" if name == "compatibility" else "report_path")
        if phase == "input" and not summary["layers"]["syntax"]["evidence_file"]:
            evidence = run_dir / "engine.log"
            if not evidence.exists() and summary.get("input_path"):
                evidence = Path(summary["input_path"])
            summary["layers"]["syntax"]["evidence_file"] = str(evidence) if evidence.is_file() else None
        serialized = json.dumps(summary, indent=2) + "\n"
        (run_dir / "summary.json").write_text(serialized)
        summary_path.write_text(serialized)
    for name, layer in summary["layers"].items():
        print(f'{name:14} {layer["status"]:11} {layer["reason"]}')
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
    argv = list(sys.argv[1:] if argv is None else argv)
    if (argv and argv[0] not in {"status", "check", "run", "release", "packages", "surface", "-h", "--help"}
            and not argv[0].startswith("-")):
        argv = ["run", "--", *argv]
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    stat = commands.add_parser("status", help="Show the five specification baselines and signal status.")
    stat.add_argument("--json", action="store_true")
    # Release surfaces. These need no credential and no environment variable:
    # GitHub is reached through the already-authenticated gh CLI, and the package
    # registries, DNS, the OIDC discovery document and the health endpoint are public.
    rel = commands.add_parser("release", help="Measure a release claim: branch, tag, release entry, run timing.")
    rel.add_argument("repo", help="owner/repo")
    rel.add_argument("version", help="Version without the leading v.")
    rel.add_argument("--branch", default="main", help="Branch head the dereferenced tag is compared against.")
    rel.add_argument("--budget", type=positive, default=10, help="Wall clock budget per run, in minutes.")
    rel.add_argument("--draft-lag", type=positive, default=2,
                     help="Minutes published_at may trail created_at before the draft window is a finding.")
    pack = commands.add_parser("packages", help="Measure published package versions against a registry.")
    pack.add_argument("version", help="Expected version.")
    pack.add_argument("package", nargs="+", help="Package ids.")
    pack.add_argument("--npm", action="store_true", help="Ask registry.npmjs.org instead of nuget.org.")
    surf = commands.add_parser("surface", help="Measure the public surfaces listed in metadata/surfaces.json.")
    surf.add_argument("id", nargs="*", help="Limit to these surface ids; default is all of them.")
    for sub in (rel, pack, surf):
        sub.add_argument("--timeout", type=positive, default=120, help="Seconds per request.")
        sub.add_argument("--json", action="store_true", help="One JSON object per finding.")
    for name in ("check", "run"):
        sub = commands.add_parser(name)
        sub.add_argument("--registry", help="Optional custom registry; default is the pinned upstream commit.")
        sub.add_argument("--baseline-registry", help="Explicit previous registry for compatibility policies.")
        sub.add_argument("--policy", action="append", default=[], help="Add a Rego file/directory; bundled advisors stay enabled.")
        sub.add_argument("--advice-data", help="Weaver glob for additional JSON/YAML Rego data.")
        sub.add_argument("--require", action="append", choices=tuple(SCOPES), default=[],
                         help="Require evidence for this layer; repeat to require several layers.")
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
    for name, command in (("release", cmd_release), ("packages", cmd_packages), ("surface", cmd_surface)):
        if args.mode == name:
            return command(args)
    # Reuse normal cleanup for CI cancellation.
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        return check(args)
    finally:
        signal.signal(signal.SIGTERM, previous)
