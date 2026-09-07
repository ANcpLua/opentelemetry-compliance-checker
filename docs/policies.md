# Checks and policies

## Included checks

| Layer | Checks |
| --- | --- |
| Syntax | Weaver sample decoding, primitive/array types, enum values, signed integer bounds, and values inconsistent with a declared type. |
| Semantics | Registered names, units, instruments, applicable attribute requirements, OTel naming rules, and HTTP request method. |
| Behavior | HTTP 1xx–3xx responses cannot use span status `Ok`; HTTP spans marked Error require a non-empty `error.type`. |
| Stability | Weaver's maturity and deprecation findings for observed definitions. |
| Compatibility | Stable attributes retain names, types, existing enum member IDs/values, and maturity. Stable metrics retain names, units, and instruments. |

HTTP rules apply to client/server spans carrying `http.request.method` or `http.response.status_code`. They follow [HTTP SemConv 1.44.0](https://github.com/open-telemetry/semantic-conventions/blob/v1.44.0/docs/http/http-spans.md#status). Error classification for 4xx/5xx responses is left to the instrumentation's context.

Compatibility requires `--baseline-registry`. Enum additions, documentation changes, and deprecation are allowed. Changes to unstable definitions are outside this policy.

## Coverage

- Syntax covers decoded samples and available type evidence. It cannot assess fields discarded by Weaver or values absent from name-only samples. OTel [empty values and null array entries](https://github.com/open-telemetry/opentelemetry-specification/blob/v1.60.0/specification/common/README.md#anyvalue) remain valid.
- Semantic requirements run where Weaver can match and evaluate them. Requirements written only as prose need explicit policies.
- Behavior covers the two HTTP relationships above. Weaver's normalized spans omit trace IDs and timestamps; propagation, retries, idempotency, and timing need other tests.
- Stability covers matched registry definitions. Unknown definitions have no maturity evidence; mixed known/unknown observations produce a warning.
- Compatibility covers the listed attributes and metric properties. Metric dimensions, spans, events, entities, SDK APIs, and wire changes need additional rules.

Registry versions and the signal-status snapshot are in [specifications.json](../metadata/specifications.json). They describe specification state, not language SDK support.

## Custom policies

Add a Rego file or directory with `--policy`. Bundled checks remain enabled.

```sh
./otel-check check \
  --policy examples/policies/service.rego \
  --advice-data 'examples/policy-data/*.json' \
  examples/valid.json
```

This example checks `service.name` against an allowlist. See the [policy](../examples/policies/service.rego) and [data](../examples/policy-data/service.json).

Live policies use Weaver's `live_check_advice` package and return standard `PolicyFinding` objects through `deny`. Inputs include `input.sample`, `input.registry_attribute`, `input.registry_group`, `input.resource`, and `input.instrumentation_scope`. `--advice-data` loads JSON/YAML through Weaver's existing glob and namespace rules; keep its built-in data keys reserved.

| Finding ID | Report layer |
| --- | --- |
| `type_mismatch`, `undefined_enum_variant` | Syntax |
| `not_stable`, `deprecated` | Stability |
| `syntax.*`, `semantics.*`, `behavior.*`, `stability.*` | Named layer |
| Other live findings | Semantics |
| Registry comparison findings | Compatibility |

Custom behavior violations are reported even without HTTP spans. A policy returning no findings does not by itself establish behavior coverage.

Comparison policies use `comparison_after_resolution`, with the candidate in `input` and baseline in `data`, in Weaver's v2 representation. Both packages can share a policy directory. Invalid policies fail the assessment. Each run snapshots and hashes its Rego files.

Upstream reference: [live advisors](https://github.com/open-telemetry/weaver/blob/38befce4bc68fe320d20f189465c93835ac20c12/crates/weaver_live_check/README.md#custom-advisors) · [baseline comparison](https://github.com/open-telemetry/weaver/blob/38befce4bc68fe320d20f189465c93835ac20c12/tests/v2_check_baseline/next/comparison_after_resolution_error.rego).

## Attribution

[upstream.rego](../policies/live/upstream.rego) is copied unchanged from Weaver's `defaults/policies/live_check_advice/otel.rego` at commit `38befce4bc68fe320d20f189465c93835ac20c12`, under [Apache-2.0](../policies/LICENSE.weaver). The copy preserves Weaver's default naming checks when loading additional advisors. Update it alongside the engine version.
