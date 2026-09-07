# Platform validation agents

The four files in [`.codex/agents`](../.codex/agents) are optional, standalone [Codex custom agent configurations](https://learn.chatgpt.com/docs/agent-configuration/subagents). Codex loads project agents from `.codex/agents/`. To use them for another platform, copy the four TOML files into that platform repository's `.codex/agents/` directory, reviewing any existing files before replacing them. They inherit the chosen model and request high reasoning effort. The checker and its GitHub Action do not use these files.

| Agent | Bounded responsibility |
| --- | --- |
| `dead_code_remover` | Prove reachability and contract boundaries before deleting code. |
| `dup_unifier` | Unify equivalent implementations while preserving independent service contracts. |
| `flake_root_causer` | Reproduce nondeterminism, fix the cause, and measure repeated validation. |
| `test_value_pruner` | Remove tests only with concrete evidence and preserved surviving coverage. |

## Starting a platform investigation

Supply the actual repository/revision, available environment, service scope, and desired work. Each agent discovers commands and contracts from that target. None assumes that a sample application or earlier example report represents the finished platform. A future platform is not scheduled or tested automatically by these configurations.

Example parent-task request, once the platform is available:

> Inspect this platform's supported test gates and run them. Use the four custom agents for separate, bounded investigations where their roles apply. Fix demonstrated defects in scope. For repeat runs, start with at most 20 iterations or 10 minutes per batch, concurrency 1; stop on the first failure to preserve evidence and investigate. Report exact commands, revision, executed/skipped counts, failures, and untested boundaries. Keep commits and publication in the parent task.

The four agents are specialists, not a substitute for a parent task running and assessing the platform's test matrix. Assign separate files or clusters when delegating implementation to avoid concurrent edits. A stress budget is a ceiling, not a requirement to consume it after sufficient evidence exists. Increase it only to answer a remaining hypothesis or within an explicit user request. Record all failures even if later iterations pass.

## Evidence and telemetry

Report each gate as **passed**, **failed**, **not run**, or **blocked**, with its command, actual executed counts, and reason where applicable. Zero discovered tests, platform skips, missing services, or required-but-absent telemetry cannot establish a pass. Keep test artifacts outside tracked source and remove only resources owned by the investigation.

For OpenTelemetry validation, run the checker around a workload that exports and flushes OTLP/gRPC:

```bash
/path/to/opentelemetry-compliance-checker/otel-check run -- your-test-command
```

Use the target's intended registry for custom attributes. Capture the registry and engine pins, expected signals, received entity counts, and the checker's summary. Specification version, specification maturity, observed telemetry conformance, and language SDK support are distinct evidence. Passing this gate establishes only the sampled behavior covered by its registry and advisors; other platform contracts require their own tests.
