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

# Query redaction. Two owners, two correct shapes, so two rules -- one cannot judge
# both. `url.full` belongs to System.Net.Http since 15.0.0: the BCL replaces the whole
# query with `*`, and tools/verify-otlp-receiver.py asserts exactly that string, so a
# per-value `key=Redacted` on url.full is a regression of that handover, not an
# alternative. `url.query` belongs to qyl on ASP.NET Core server spans: it carries no
# `?`, and every pair must end in `=Redacted`.
#
# Both rules assume redaction is on. System.Net.Http.DisableUriRedaction and
# OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_DISABLE_URL_QUERY_REDACTION turn it off on
# purpose, and a run with either set produces correct raw queries. A span carries no
# trace of the switch, so the harness must never live-check such a run. A rule that
# reds on a supported configuration is a rule people learn to ignore.

# The query is what follows the first `?` after the fragment is removed --
# `https://x/a#b?c` has no query at all.
query_of(url) := q if {
	before_fragment := split(url, "#")[0]
	i := indexof(before_fragment, "?")
	i >= 0
	q := substring(before_fragment, i + 1, -1)
}

# Stated positively, so that "not redacted" is whatever fails to match rather than a
# list of shapes someone remembered to forbid.
full_redacted(q) if q == ""
full_redacted(q) if q == "*"

pair_redacted(p) if {
	i := indexof(p, "=")
	i > 0
	substring(p, i + 1, -1) == "Redacted"
}

query_redacted(q) if q == ""
query_redacted(q) if {
	q != ""
	every p in split(q, "&") { pair_redacted(p) }
}

deny contains {
	"id": "behavior.attribute.unredacted_query",
	"level": "violation",
	"message": "CLIENT span carries an unredacted query in url.full. Since 15.0.0 the BCL owns this lane and replaces the whole query with '*'; anything else means redaction did not reach this copy of the request.",
	"context": {"attribute_key": "url.full"},
	"signal_type": "span",
	"signal_name": input.sample.span.name,
} if {
	input.sample.span.kind == "client"
	some attr in input.sample.span.attributes
	attr.name == "url.full"
	q := query_of(attr.value)
	not full_redacted(q)
}

deny contains {
	"id": "behavior.attribute.unredacted_url_query",
	"level": "violation",
	"message": "SERVER span carries an unredacted value in url.query. qyl redacts per value, so every pair must end in '=Redacted'; a segment without '=' is returned untouched by the current helper and reaches the tag raw.",
	"context": {"attribute_key": "url.query"},
	"signal_type": "span",
	"signal_name": input.sample.span.name,
} if {
	input.sample.span.kind == "server"
	some attr in input.sample.span.attributes
	attr.name == "url.query"
	not query_redacted(attr.value)
}
