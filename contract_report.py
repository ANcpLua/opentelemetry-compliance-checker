"""Evidence-based layers over Weaver's existing reports and PolicyFinding format."""

from collections import Counter

LEVELS = {"information": 1, "improvement": 2, "violation": 3}
SCOPES = {
    "syntax": "Weaver sample decoding and supplied attribute types/enum values; not full OTLP wire conformance.",
    "semantics": "Observed names, units, instruments, and available registry/advisor requirements.",
    "behavior": "Identified HTTP client/server spans: 1xx-3xx status and Error/error.type relationships.",
    "stability": "Maturity/deprecation of observed definitions matched by Weaver's registry advisors.",
    "compatibility": "Stable baseline attributes: presence/type/maturity; stable metrics: presence/unit/instrument.",
}


def new_layers():
    return {name: {"status": "NOT_CHECKED", "scope": scope, "observations": 0,
                   "reason": "No evidence yet.", "findings": []}
            for name, scope in SCOPES.items()}


def finding_layer(finding):
    prefix = finding["id"].split(".", 1)[0]
    if prefix in SCOPES and prefix != "compatibility":
        return prefix
    if finding["id"] in {"type_mismatch", "undefined_enum_variant"}:
        return "syntax"
    if finding["id"] in {"not_stable", "deprecated"}:
        return "stability"
    return "semantics"


def add_finding(layer, finding, path):
    if (not isinstance(finding, dict) or not isinstance(finding.get("id"), str)
            or finding.get("level") not in LEVELS or not isinstance(finding.get("message"), str)):
        raise ValueError("Malformed Weaver PolicyFinding; cannot evaluate the contract.")
    layer["findings"].append({**finding, "evidence_path": path})


def assessed(layer, observations, reason):
    layer.update(observations=observations, reason=reason)
    if any(f["level"] == "violation" for f in layer["findings"]):
        layer["status"] = "FAIL"
    elif layer["findings"]:
        layer["status"] = "WARN"
    else:
        layer["status"] = "PASS" if observations else "NOT_CHECKED"


def entities(node, path="/samples"):
    """Visit normalized entities, never interpret application payloads as findings."""
    if isinstance(node, list):
        for index, value in enumerate(node):
            yield from entities(value, f"{path}/{index}")
    elif isinstance(node, dict):
        if isinstance(node.get("live_check_result"), dict):
            yield node, path
        for key, value in node.items():
            if key not in {"live_check_result", "value", "body"}:
                yield from entities(value, f"{path}/{key}")


def read_telemetry(report, layers):
    stats = report["statistics"]
    total = stats["total_entities"]
    if type(total) is not int or total <= 0:
        raise ValueError("No telemetry was observed. A check with zero samples cannot pass.")
    if not isinstance(report.get("samples"), list) or not report["samples"]:
        raise ValueError("Weaver report has no samples to substantiate its statistics.")
    normalized = list(entities(report["samples"]))
    if not normalized:
        raise ValueError("Weaver report has no assessed entities.")
    findings = []
    for entity, path in normalized:
        advice = entity["live_check_result"].get("all_advice")
        if not isinstance(advice, list):
            raise ValueError("Weaver report is missing entity findings.")
        for index, finding in enumerate(advice):
            # Validate before choosing a layer, including custom policy output.
            validated = {"findings": []}
            add_finding(validated, finding, f"{path}/live_check_result/all_advice/{index}")
            layers[finding_layer(finding)]["findings"].extend(validated["findings"])
            findings.append(finding)
    counts = dict(Counter(f["level"] for f in findings))
    if counts != stats["advice_level_counts"]:
        raise ValueError("Weaver entity findings do not match its aggregate statistics.")

    http_spans = [e for e, _ in normalized if e.get("kind") in {"client", "server"}
                  and any(a.get("name") in {"http.request.method", "http.response.status_code"}
                          for a in e.get("attributes", []))]
    known = sum(sum(stats.get(key, {}).values()) for key in
                ("seen_registry_attributes", "seen_registry_metrics", "seen_registry_events"))
    unknown = sum(sum(stats.get(key, {}).values()) for key in
                  ("seen_non_registry_attributes", "seen_non_registry_metrics", "seen_non_registry_events"))
    assessed(layers["syntax"], total, "Decoded samples and checked supplied attribute values.")
    assessed(layers["semantics"], known + unknown + len(http_spans),
             "Only registry/advisor rules applicable to the observed telemetry were evaluated.")
    assessed(layers["behavior"], len(http_spans),
             "Evaluated the two bundled HTTP relationship rules." if http_spans else
             "No HTTP client/server spans identified; workload exit alone is not behavior evidence.")
    assessed(layers["stability"], known,
             f"{known} matched definitions; {unknown} unknown observations have no maturity evidence.")
    if unknown and layers["stability"]["status"] == "PASS":
        layers["stability"]["status"] = "WARN"
    return total, counts


def read_compatibility(diagnostics, code, layer):
    """A successful comparison with explicit baseline is evidence even when unchanged."""
    if not isinstance(diagnostics, list):
        raise ValueError("Expected a Weaver diagnostic array for the registry comparison.")
    errors = []
    for index, item in enumerate(diagnostics):
        error = item.get("error", {})
        finding = error.get("violation") if isinstance(error, dict) else None
        if isinstance(finding, dict):
            add_finding(layer, finding, f"/{index}/error/violation")
        elif item.get("diagnostic", {}).get("severity", "").lower() == "error":
            errors.append(item["diagnostic"].get("message", "Registry resolution failed."))
    if errors or (code and not layer["findings"]):
        layer.update(status="ERROR", reason="; ".join(errors) or
                     "Weaver registry comparison failed without a policy finding.")
    else:
        assessed(layer, 1, "Compared explicit baseline and candidate with the bundled evolution policy.")


def finish(summary, operational_error=False, cancelled=False):
    layers = summary["layers"]
    missing = [name for name in summary["required_layers"] if layers[name]["status"] == "NOT_CHECKED"]
    summary["unassessed_layers"] = [name for name, layer in layers.items() if layer["status"] == "NOT_CHECKED"]
    summary["fully_evaluated"] = all(layer["status"] in {"PASS", "WARN", "FAIL"} for layer in layers.values())
    gate_findings = [f for layer in layers.values() for f in layer["findings"]
                     if LEVELS[f["level"]] >= LEVELS[summary["fail_on"]]]
    summary["blocking_findings"] = len(gate_findings)
    if cancelled:
        summary.update(state="cancelled", exit_code=130)
    elif operational_error or any(layer["status"] == "ERROR" for layer in layers.values()):
        summary.update(state="error", exit_code=2)
    elif missing:
        summary.update(state="incomplete", exit_code=2,
                       error="Required layers lack evidence: " + ", ".join(missing))
    elif gate_findings or summary.get("workload_exit_code") not in (None, 0):
        summary.update(state="failed", exit_code=1)
    else:
        summary.update(state="passed", exit_code=0)
    return summary["exit_code"]
