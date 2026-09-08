package live_check_advice

import rego.v1

# Instrumentation regression guards. Unlike contract.rego these do not restate a
# specification requirement; they pin defects that shipped once and must not return.
# Both are single-sample assertions -- live-check evaluates one sample at a time, so a
# rule about a *set* of spans (two CLIENT spans for one request) has no expression here
# and stays in the harness that owns the set.

# A request whose route was never resolved keeps the framework's raw operation name,
# because the display name is only refined once the endpoint is known. Every 404, every
# static file, every pre-routing abort exports it.
framework_server_names := {"Microsoft.AspNetCore.Hosting.HttpRequestIn"}

deny contains {
	"id": "behavior.span_name.unresolved_route",
	"level": "violation",
	"message": sprintf("SERVER span exports the raw framework name '%s'. The route was not resolved when the display name was set -- a 404, a static file, or a pre-routing abort.", [input.sample.span.name]),
	"context": {"span_name": input.sample.span.name},
	"signal_type": "span",
	"signal_name": input.sample.span.name,
} if {
	input.sample.span.kind == "server"
	input.sample.span.name in framework_server_names
}

# Redaction that runs on one copy of an outgoing request does not reach a second copy
# emitted by another source. A query string surviving in url.full is that second copy.
deny contains {
	"id": "behavior.attribute.unredacted_query",
	"level": "violation",
	"message": "CLIENT span carries a query string in url.full. Redaction did not reach this copy of the outgoing request.",
	"context": {"attribute_key": "url.full"},
	"signal_type": "span",
	"signal_name": input.sample.span.name,
} if {
	input.sample.span.kind == "client"
	some attr in input.sample.span.attributes
	attr.name == "url.full"
	contains(attr.value, "?")
}
