# opentelemetry-compliance-checker
Description
Checks OpenTelemetry compliance based on the official glossary. Use when reviewing code, docs, or configs for correct terminology, spelling, capitalization, and adherence to OTEL concepts like Attributes, Signals, Semantic Conventions, OTel spelling rules. Triggers include opentelemetry review, otel compliance, glossary check, terminology.

# Opentelemetry Compliance Checker

## Overview

This skill ensures strict adherence to OpenTelemetry glossary rules, terminology, spelling, and conventions in code, documentation, and configurations.

## Instructions

When reviewing for OpenTelemetry compliance:

1. Verify correct spelling and capitalization:
   - OpenTelemetry (always one word, capitalized)
   - OTel (not OTEL or otel)
   - Collector (capital C when referring to OpenTelemetry Collector)
   - OpAMP, OTEP (specific casing)
   - OTLP, SDK, API, etc.

2. Use precise terminology:
   - Attribute (not Tag, Label, Metadata in OTEL context)
   - Signal (Traces, Metrics, Logs)
   - Span, Trace, Resource, Entity, Semantic Conventions, etc.
   - Instrumented library vs Instrumentation library

3. Check conceptual adherence: Context propagation, Baggage, Cardinality management, Semantic conventions usage.

4. For code/docs: Flag incorrect terms, suggest fixes, provide examples from glossary.

Reference the bundled glossary in references/glossary.md for full details.

Structure reviews as:
- Compliance Summary
- Specific violations with quotes and corrections
- Recommendations
- Overall adherence score

Always be precise and helpful for maintaining high-quality OTEL implementations.
