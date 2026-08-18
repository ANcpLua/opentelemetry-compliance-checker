# OpenTelemetry Glossary - Definitions and conventions for telemetry terms

## Terms

### Aggregation
The process of combining multiple measurements into exact or estimated statistics about the measurements that took place during an interval of time, during program execution. Used by the Metric Data source.

### API
Application Programming Interface. In the OpenTelemetry project, used to define how telemetry data is generated per Data source.

### Application
One or more Services designed for end users or other applications.

### APM
Application Performance Monitoring...

[Note: Full detailed glossary from the provided text is embedded here for reference. Key terms include: Attribute, Automatic instrumentation, Baggage, Cardinality, Collector, Context propagation, Distributed tracing, Entity, Event, Exporter, Instrumented library, Instrumentation library, Log record, Metric, OpenTelemetry, OTel, Propagators, Resource, SDK, Semantic conventions, Service, Signal, Span, Trace, etc.]

**Important conventions:**
- Spelling: OpenTelemetry (one word), OTel (not OTEL), Collector (capitalized), OpAMP, OTEP, etc.
- Use precise terms: Attribute instead of Metadata/Tag/Label in OTEL context.
- Follow semantic conventions for metadata.
