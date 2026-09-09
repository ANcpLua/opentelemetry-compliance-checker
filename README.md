# OpenTelemetry Compliance Checker

[![CI](https://github.com/ANcpLua/opentelemetry-compliance-checker/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ANcpLua/opentelemetry-compliance-checker/actions/workflows/ci.yml)

Check telemetry from your tests against OpenTelemetry semantic conventions.
Powered by [Weaver](https://github.com/open-telemetry/weaver), with separate results for syntax, semantics, behavior, stability, and compatibility.

## GitHub Actions

Add a step after checking out your application and installing its test tooling:

```yaml
- uses: ANcpLua/opentelemetry-compliance-checker@main
  with:
    run: dotnet test
```

The action installs Weaver and runs your tests with a local OTLP/gRPC endpoint.
Your application must export to that endpoint and flush telemetry before exiting.
Use any test command in place of `dotnet test`. Pin the action to a commit for reproducible builds.

## Local usage

Requires Python 3.9+ and [Weaver 0.26.1](https://github.com/open-telemetry/weaver/releases/tag/v0.26.1) on `PATH`.
Live checks support Linux and macOS.

```sh
git clone https://github.com/ANcpLua/opentelemetry-compliance-checker.git
cd opentelemetry-compliance-checker
./otel-check check examples/http-valid.json
```

Results include:

```text
syntax         PASS
semantics      PASS
behavior       PASS
stability      PASS
compatibility  NOT_CHECKED
```

To check an application, run `/path/to/otel-check dotnet test` from its test directory.
Reports are written to `otel-report/summary.json`.
A missing baseline produces `NOT_CHECKED`; zero telemetry fails the check.

## Release surfaces

The same binary also measures what a release actually put in front of users, and
reports the measured number with the source it came from.

```sh
./otel-check release ANcpLua/Qyl.OpenTelemetry.AutoInstrumentation 20.0.0
./otel-check packages 20.0.0 Qyl.Telemetry.AutoInstrumentation
./otel-check surface
```

`release` resolves the branch head, dereferences the tag — an annotated tag names
a tag object, not a commit — checks the release entry including how long it sat as
a draft, and separates each workflow run's wall clock from its compute time and
its queue time, per runner label. `packages` asks the registry, where *not
published* is a different state from *wrong version*. `surface` measures the
public npm, DNS, HTTP, OIDC and health endpoints listed in
[metadata/surfaces.json](metadata/surfaces.json).

These commands need **no environment variable and no token**. GitHub is reached
through the `gh` CLI, which already holds its own credential; package registries,
DNS, the OIDC discovery document and the health endpoint are all public. A surface
that cannot be read without a login is not checked here at all. If `gh` is missing
or logged out, `release` reports `UNKNOWN` and says so — a missing prerequisite is
never dressed up as a finding.

Anything that cannot be measured is `unknown` and colours nothing green; an
unfinished workflow run has no wall clock and gets its own `UNKNOWN` line rather
than passing the budget check. Exit code is 0 only when every check passed.

## Documentation

- [Usage](docs/usage.md) — commands, Action inputs, reports, and exit codes.
- [Checks and policies](docs/policies.md) — implemented rules, coverage, and custom Rego.
- [Specification versions](metadata/specifications.json) — pinned versions and official sources.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The tests require Weaver. CI runs on Linux and macOS.
