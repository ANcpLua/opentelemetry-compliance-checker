# Usage

## Commands

```sh
./otel-check dotnet test
./otel-check check telemetry.json
./otel-check status --json
```

Live mode starts an OTLP/gRPC receiver and sets the standard exporter endpoint and protocol variables for the child process. The application must honor those variables and flush its exporter before exit. Container workloads need an explicit route to the receiver.

File input is a [Weaver sample array](../examples/valid.json). Raw OTLP JSON uses a different format.

Use `run` when passing checker options. Arguments after `--` belong to the test command:

```sh
./otel-check run --registry ./model --require behavior -- dotnet test
./otel-check check --registry ./model --baseline-registry ./previous-model telemetry.json
```

## Options

| CLI | Action input | Default |
| --- | --- | --- |
| `check <file>` | `file` | `telemetry.json` |
| `run -- <command>` | `run` | None |
| `--registry` | `registry` | Pinned Semantic Conventions registry |
| `--baseline-registry` | `baseline-registry` | None |
| `--policy` | `policies` | Bundled policies only |
| `--advice-data` | `advice-data` | None |
| `--require` | `require` | None |
| `--fail-on` | `fail-on` | `violation` |
| `--timeout` | `timeout` | 300 seconds per workload, file check, or comparison |
| `--startup-timeout` | — | 60 seconds |
| `--output` | `output` | `otel-report` |

Repeat `--policy` for multiple paths and `--require` for multiple layers. In the Action, `policies` takes one path per line and `require` takes space-separated layer names. `run` takes precedence over `file`.

`--fail-on` accepts `violation`, `improvement`, or `information`. `--require` accepts `syntax`, `semantics`, `behavior`, `stability`, or `compatibility` and fails when that layer has no evidence.

```yaml
- uses: ANcpLua/opentelemetry-compliance-checker@main
  with:
    run: dotnet test
    registry: ./telemetry/model
    baseline-registry: ./telemetry/previous-model
    require: behavior compatibility
```

## Release surfaces

```sh
./otel-check release <owner/repo> <version> [--branch main] [--budget 10] [--draft-lag 2] [--json]
./otel-check packages <version> <package>... [--npm] [--json]
./otel-check surface [<id>...] [--json]
```

| CLI | Default | Meaning |
| --- | --- | --- |
| `--branch` | `main` | Branch head the dereferenced tag is compared against. |
| `--budget` | 10 | Wall clock budget per workflow run, in minutes. |
| `--draft-lag` | 2 | Minutes `published_at` may trail `created_at` before the draft window is reported. |
| `--npm` | off | Ask `registry.npmjs.org` instead of `nuget.org`. |
| `--timeout` | 120 | Seconds per request. |
| `--json` | off | One JSON object per finding, instead of the human report. |

No environment variable is required, and none is read. GitHub goes through the
already-authenticated `gh` CLI; every other surface answers unauthenticated. No
dashboard, management API, or project token is contacted.

`surface` takes its targets from [`metadata/surfaces.json`](../metadata/surfaces.json),
which records identities only — package names, URLs and the OIDC issuer. It holds
no version: a version in a file is a claim, and the registry answer is the
evidence. The host name is derived from the URL and the discovery path from the
issuer, so no target is written twice. Pass surface ids to check a subset.

| Finding status | Meaning |
| --- | --- |
| `OK` | Measured, and it matched. |
| `WARN` | Measured, and it is a finding (for example a release left as a draft). |
| `FAIL` | Measured, and it did not match. |
| `UNKNOWN` | Not measurable. Never green, and never counted as a finding either. |
| `INFO` | Context that does not gate the result. |

Wall clock exists only for a completed run: `updated_at` on a running workflow is
the last time GitHub touched it, not its end, so subtracting `created_at` yields a
wall clock shorter than the compute already spent. An unfinished run therefore
keeps no wall clock and gets its own `UNKNOWN` line. The start delay of a runner
label uses the *first* start of that label, because a later start may have waited
on an upstream job rather than on a runner.

## Reports

The Action displays results in the job summary and exposes the report path through its `summary` output.

| File | Contents |
| --- | --- |
| `otel-report/summary.json` | Latest result, using [summary schema v2](../schemas/summary.schema.json) |
| `otel-report/run-*/summary.json` | Result retained for that invocation |
| `otel-report/run-*/live_check.json` | Weaver samples and findings |
| `otel-report/run-*/engine.log` | Engine diagnostics |
| `otel-report/run-*/compatibility.log` | Baseline comparison diagnostics, when requested; empty if there are none |
| `otel-report/run-*/policies/` | Snapshot of the evaluated Rego files |

Each layer records its status, checked scope, observation count, and findings. `evidence_file` and `evidence_path` locate a finding in the source report. Policy hashes and registry references are recorded in the summary.

| Status | Meaning |
| --- | --- |
| `PASS` | Applicable checks completed without findings. |
| `WARN` | Non-violation findings or partial maturity coverage. |
| `FAIL` | A rule violation or malformed input. |
| `NOT_CHECKED` | Required evidence was absent. |
| `ERROR` | Assessment could not complete. |

Statuses describe the [implemented checks](policies.md), not complete protocol certification. `fully_evaluated` means every layer has a result within that scope; it can still include failures. Compatibility counts compared registry pairs. File checks have a null `workload_exit_code`.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Observed telemetry, required layers, and selected threshold passed; any workload succeeded. |
| `1` | A finding met the failure threshold, or the workload failed. |
| `2` | Invalid input/configuration, missing evidence for a required layer, zero telemetry, or an execution error/timeout. |
| `130` | Interrupted. |

A strict threshold can fail the process while a layer remains `WARN`. Missing baselines yield `NOT_CHECKED`; invalid baselines yield `ERROR`.
