# OpenTelemetry compliance checker

A per-service **contract/compliance engine** powered by **Weaver (Rust + Rego)**. It reports syntax, semantics, behavior, stability, and compatibility separately. Python 3.9+ and Weaver 0.26.1 are the only local prerequisites; the GitHub Action installs Weaver.

```bash
./otel-check dotnet test
```

The command starts an isolated local OTLP/gRPC receiver, passes its endpoint to the test process through standard OTel environment variables, waits for the process, and checks the observed telemetry. Use any test command in place of `dotnet test`.

The default registry is fetched from an exact upstream commit; use `--registry /path/to/model` for a local registry. No Qyl package, language-specific analyzer, database, or running backend is required.

## Separate evidence for each layer

| Layer | Implemented checks |
| --- | --- |
| Syntax | Weaver sample decoding, supplied primitive/array types and enum values; detects misleading declared types. |
| Semantics | Registry names, units, instruments, available requirement rules, and required HTTP method. |
| Behavior | HTTP 1xx–3xx span-status relationship; `error.type` on HTTP spans marked Error. |
| Stability | Maturity and deprecation findings for observed definitions matched by Weaver. |
| Compatibility | Explicit baseline comparison: stable attribute presence/type/maturity and metric presence/unit/instrument. |

For example, `./otel-check check examples/http-valid.json` reports:

```text
syntax         PASS
semantics      PASS
behavior       PASS
stability      PASS
compatibility  NOT_CHECKED
```

`PASS` applies to the documented rules and available observations. `NOT_CHECKED` means evidence is absent. A successful test process alone does not prove behavioral correctness; compatibility needs a real previous registry. Require a layer when its absence must block CI:

```bash
./otel-check run --require behavior --baseline-registry ./previous-model --require compatibility -- dotnet test
```

Read [the contract and policy reference](docs/contracts.md) for exact scope, evidence, extension points, and limitations. Weaver registry YAML and Rego express rules; JSON Schema describes the report. There is no new DSL.

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

Optional inputs: `file`, `run`, `registry`, `baseline-registry`, `policies`, `advice-data`, `require`, `fail-on`, `timeout`, `output`. Additional policies are newline-separated paths; required layers are space-separated names. Defaults remain `telemetry.json`, the pinned registry, `violation`, 300 seconds, and `otel-report`; baseline and custom policies are optional. Results also appear in the GitHub job summary.

```yaml
- uses: ANcpLua/opentelemetry-compliance-checker@main
  with:
    run: dotnet test
    registry: ./telemetry/model
    baseline-registry: ./telemetry/previous-model
    require: behavior compatibility
```

## Local commands

```bash
./otel-check status
./otel-check status --json
./otel-check check examples/valid.json
./otel-check run -- dotnet test
./otel-check check --fail-on information telemetry.json
./otel-check check --registry ./registry telemetry.json
./otel-check check --policy examples/policies/service.rego --advice-data 'examples/policy-data/*.json' examples/valid.json
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

Weaver evaluates sampled telemetry with built-in advisors, pinned upstream naming policies, and the bundled contract policies. The default gate fails on violations; `--fail-on improvement` or `information` also rejects less severe findings. Layer statuses preserve their meaning: a stability `WARN` stays a `WARN` even when a strict threshold makes the overall gate fail.

This engine does not analyze C# syntax or certify complete OTel/OTLP/OpAMP conformance. Its normalized span evidence does not retain trace IDs or timestamps, so propagation, retries, temporal state transitions, and idempotency are not established by the bundled checks. Compatibility covers the stated registry evolution rules, not every API lifecycle or wire change. Registry coverage is reported, not assumed.

Qyl's Roslyn analyzer remains a complementary compile-time check. This repository has no dependency on it. [TypeSpec](https://typespec.io/data-validation/) can define a future public service contract and generate JSON Schema; it is unnecessary for the existing Weaver input/report contract.

## Reports and failures

Each invocation creates a fresh run directory with a complete `live_check.json`, diagnostics, a snapshot of all Rego policies, and an immutable `summary.json`. `otel-report/summary.json` contains the latest summary. A requested baseline comparison also writes `compatibility.log` with Weaver's JSON diagnostics.

The [version 2 JSON Schema](schemas/summary.schema.json) specifies the summary. Each layer includes status, scope, observation count, reason, findings, and its evidence file. Findings retain Weaver's IDs, levels, context, and JSON pointers into the evidence. Policy hashes, registry references, workload exit, and unassessed layers make the result reviewable. A missing baseline defaults to `NOT_CHECKED`; an invalid baseline is `ERROR`.

- Exit 0: telemetry was observed, any workload succeeded, requested layers had evidence, and the selected finding threshold passed.
- Exit 1: a finding met the selected threshold or the workload failed after telemetry was observed.
- Exit 2: invalid/missing input or engine, invalid policy/registry/report, zero telemetry, startup failure, timeout, or missing evidence for a required layer.
- Exit 130: interrupted execution.

An empty file/array, zero observed telemetry, a timed-out process, or a failing workload never produces a passing summary. An ambient `.weaver.toml` cannot silently disable the check. Live subprocess groups are cleaned up on completion, errors, and cancellation.

## Development

Four optional Codex agents in [.codex/agents](.codex/agents) support platform cleanup and test investigations: dead code, duplicate implementations, flaky tests, and test value. See [the platform validation guide](docs/platform-validation.md) for setup, scope, and evidence requirements. They are independent of the checker and introduce no runtime dependency.

```bash
python3 -m unittest discover -s tests -v
```

The integration tests use the real pinned engine and test-only registries. They cover live OTLP, failure handling, policy composition, layer isolation, and compatible/breaking registry changes. CI additionally requires four evidenced layers on the HTTP sample and rejects an invalid attribute type against the pinned upstream registry on Linux and macOS.

CI uses standard `ubuntu-latest` and `macos-latest` runners, whose execution is [free for public repositories](https://docs.github.com/en/billing/concepts/product-billing/github-actions). Each job installs Weaver once through this action, reusing the upstream binary cache keyed by version, OS, and architecture. Runs trigger on pushes to `main`, pull requests, or manual dispatch; a newer run cancels the previous run for that branch or PR. Feature branches use the PR check to avoid duplicate push/PR runs. Both operating systems retain the full test suite and upstream checks, with a five-minute job limit.

The original `SKILL.md` and incomplete `glossary.md` are retained as legacy material; neither drives the executable checker.
