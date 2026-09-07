package live_check_advice

import rego.v1

# Weaver accepts caller-supplied types; validate the underlying value too.
contract_value_matches(kind, value) if { contract_scalar_matches(kind, value) }
contract_value_matches(kind, value) if {
    endswith(kind, "[]")
    is_array(value)
    scalar := trim_suffix(kind, "[]")
    every item in value { contract_scalar_matches(scalar, item) }
}

contract_scalar_matches("string", value) if { is_string(value) }
contract_scalar_matches("boolean", value) if { is_boolean(value) }
contract_scalar_matches("int", value) if {
    is_number(value)
    value == floor(value)
    value >= -9223372036854775808
    value <= 9223372036854775807
}
contract_scalar_matches("double", value) if { is_number(value) }
# OTel permits empty values, including null entries preserved in homogeneous arrays.
contract_scalar_matches(kind, value) if { kind in {"string", "boolean", "int", "double"}; value == null }

contract_primitive_types := {"string", "boolean", "int", "double", "string[]", "boolean[]", "int[]", "double[]"}
contract_expected_type := input.sample.attribute.type if {
    input.sample.attribute.type in contract_primitive_types
} else := input.registry_attribute.type if {
    input.registry_attribute.type in contract_primitive_types
}

deny contains {
    "id": "syntax.attribute_value", "level": "violation",
    "message": "The supplied attribute value does not match its declared or inferred type.",
    "context": {"attribute_key": attr.name, "expected_type": expected},
} if {
    attr := input.sample.attribute
    attr.value != null
    expected := contract_expected_type
    not contract_value_matches(expected, attr.value)
}

contract_http_span if {
    input.sample.span.kind in {"client", "server"}
    some attr in input.sample.span.attributes
    attr.name in {"http.request.method", "http.response.status_code"}
}

contract_has_attr(key) if {
    some attr in input.sample.span.attributes
    attr.name == key
    is_string(attr.value)
    attr.value != ""
}

deny contains {
    "id": "semantics.http_method", "level": "violation",
    "message": "An identified HTTP client/server span needs http.request.method.",
    "signal_type": "span", "signal_name": input.sample.span.name,
    "context": {"source": "https://github.com/open-telemetry/semantic-conventions/blob/v1.44.0/docs/http/http-spans.md"},
} if {
    contract_http_span
    not contract_has_attr("http.request.method")
}

# Other failures can justify Error even with a 1xx-3xx response.
deny contains {
    "id": "behavior.http_success_status", "level": "violation",
    "message": "HTTP 1xx-3xx spans must use Unset, or Error for another failure; not Ok.",
    "signal_type": "span", "signal_name": input.sample.span.name,
    "context": {"source": "https://github.com/open-telemetry/semantic-conventions/blob/v1.44.0/docs/http/http-spans.md#status"},
} if {
    contract_http_span
    some attr in input.sample.span.attributes
    attr.name == "http.response.status_code"
    is_number(attr.value)
    attr.value >= 100
    attr.value < 400
    input.sample.span.status.code == "ok"
}

# The instrumentation decides whether a 4xx/5xx response is an error.
deny contains {
    "id": "behavior.http_error_type", "level": "violation",
    "message": "An HTTP span marked Error needs a non-empty error.type attribute.",
    "signal_type": "span", "signal_name": input.sample.span.name,
    "context": {"source": "https://github.com/open-telemetry/semantic-conventions/blob/v1.44.0/docs/http/http-spans.md"},
} if {
    contract_http_span
    input.sample.span.status.code == "error"
    not contract_has_attr("error.type")
}
