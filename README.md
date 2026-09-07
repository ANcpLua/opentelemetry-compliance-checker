# OpenTelemetry compliance checker

A small per-service CI check powered by **Weaver (Rust + Rego)**. No Qyl package, language-specific analyzer, database, or running backend is required.

```bash
./otel-check run -- dotnet test
```

The command starts an isolated local OTLP/gRPC receiver, passes its endpoint to the test process through standard OTel environment variables, waits for the process, and checks the observed telemetry. Use any test command in place of `dotnet test`.

Prerequisites locally: Python 3.9+ and Weaver **0.26.1** on PATH. The GitHub Action installs Weaver. The default registry is fetched from an exact upstream commit; use `--registry /path/to/model` for a local registry.

## GitHub Actions

Add one step after checkout and a test step that produces `telemetry.json`:

```yaml
- uses: ANcpLua/opentelemetry-compliance-checker@main
```

Here `telemetry.json` means a **Weaver sample JSON array**, as in [examples/valid.json](examples/valid.json). Raw OTLP export JSON has a different structure and is not accepted as a sample file.

For existing tests that export OTLP, let the action run them:

```yaml
- uses: ANcpLua/opentelemetry-compliance-checker@main
  with:
    run: dotnet test
```

The caller still checks out the service and installs its own build/test tooling. Pin a published commit SHA for reproducible use. No `v1` release is assumed to exist.

Optional inputs: `file`, `run`, `registry`, `fail-on`, `timeout`, `output`. The defaults are `telemetry.json`, no command, the pinned upstream registry, `violation`, 300 seconds, and `otel-report`.

## Local commands

```bash
./otel-check status
./otel-check status --json
./otel-check check examples/valid.json
./otel-check run -- dotnet test
./otel-check check --fail-on information telemetry.json
./otel-check check --registry ./registry telemetry.json
```

Live mode supports Linux/macOS and child processes on the same host. The application needs an OTLP/gRPC exporter that honors the supplied environment variables and flushes its SDK before exit. Explicit endpoints configured in application code can override environment settings. Separate containers must be configured to reach the receiver; the wrapper does not reconfigure container networks.

File mode also works without an instrumented application. Use an explicit custom registry for your own attributes; unknown names produce Weaver findings against the selected registry.

## Five metadata sources

The baseline and signal snapshot live in [metadata/specifications.json](metadata/specifications.json).

| Metadata | Version | Status granularity | URL |
| --- | --- | --- | --- |
| Status summary | Snapshot 2026-09-07 | Signal × API / SDK / protocol | https://opentelemetry.io/docs/specs/status/ |
| OpenTelemetry specification | 1.60.0 | Per section | https://opentelemetry.io/docs/specs/otel/ |
| OTLP | 1.11.0 | Per signal | https://opentelemetry.io/docs/specs/otlp/ |
| OpAMP | Not stated in page title | Per message / field | https://opentelemetry.io/docs/specs/opamp/ |
| Semantic Conventions | 1.44.0 | Per definition | https://opentelemetry.io/docs/specs/semconv/ |

| Signal | API | SDK | Protocol |
| --- | --- | --- | --- |
| Traces | stable | stable | stable |
| Metrics | stable | mixed | stable |
| Baggage | stable | stable | not applicable |
| Logs | stable (Bridge API) | stable | stable |
| Profiles | not stated | not stated | development |

These are the specification summary's values, not a language SDK compatibility matrix. Version and maturity are separate facts.

OpenTelemetry is pinned to [v1.60.0](https://github.com/open-telemetry/opentelemetry-specification/tree/v1.60.0) as a reference. Execution uses Weaver 0.26.1 and the complete Semantic Conventions 1.44.0 registry at `e10a930844c6951757a43b849d364f7d056ac32b`. The other specification versions are reference metadata, not additional implemented validators. Metadata is a checked-in snapshot; updating it is an explicit reviewable change.

## What the check establishes

Weaver evaluates sampled telemetry, including attribute types, deprecated definitions, stability findings, and semantic rules available in its registry/advisors. The default fails on violations; `--fail-on improvement` or `information` also rejects less severe findings.

This is a telemetry validation gate. It does not analyze C# syntax, prove every API lifecycle rule, certify complete OTel/OTLP/OpAMP conformance, or treat “experimental → stable → deprecated” as a numeric compatibility test. Registry coverage is reported, not assumed.

Qyl's Roslyn analyzer remains a complementary compile-time check. This repository has no dependency on it. [TypeSpec](https://typespec.io/data-validation/) can define a future public service contract and generate JSON Schema; it is unnecessary for the existing Weaver input/report contract.

## Reports and failures

Each invocation creates a fresh run directory, a complete `live_check.json`, engine diagnostics, and `otel-report/summary.json` pointing to that run.

- Exit 0: telemetry was observed, the workload succeeded, and the selected finding threshold passed.
- Exit 1: Weaver or the workload returned a failure after telemetry was observed.
- Exit 2: missing/invalid input, missing engine, missing report, zero telemetry, startup failure, or timeout.
- Exit 130: interrupted execution.

An empty file/array, zero observed telemetry, a timed-out process, or a failing workload never produces a passing summary. An ambient `.weaver.toml` cannot silently disable the check. Live subprocess groups are cleaned up on completion, errors, and cancellation.

## Development

Four optional Codex agents in [.codex/agents](.codex/agents) support platform cleanup and test investigations: dead code, duplicate implementations, flaky tests, and test value. See [the platform validation guide](docs/platform-validation.md) for setup, scope, and evidence requirements. They are independent of the checker and introduce no runtime dependency.

```bash
python3 -m unittest discover -s tests -v
```

The integration tests use the real pinned engine and a small test-only registry. CI additionally checks valid and invalid samples against the pinned upstream registry on Linux and macOS.

The original `SKILL.md` and incomplete `glossary.md` are retained as legacy material; neither drives the executable checker.
