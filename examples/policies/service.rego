package live_check_advice

import rego.v1

# Existing Weaver PolicyFinding output and JSON data; no new contract language.
deny contains {
    "id": "semantics.service_allowlist", "level": "violation",
    "message": "service.name is outside the supplied allowlist.",
    "context": {"attribute_key": "service.name"},
} if {
    input.sample.attribute.name == "service.name"
    not input.sample.attribute.value in data.service.allowed_names
}
