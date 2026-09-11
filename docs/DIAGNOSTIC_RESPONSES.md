# Evidence-grounded troubleshooting

Day 15 adds an opt-in presentation mode to `query_wms_knowledge`:

```json
{"query": "Why is Set Down missing?", "response_format": "troubleshooting"}
```

Omit `response_format` (or use `evidence`) to retain the existing response.
The usual collection/domain/document-type/process-code filters and Workspace
retrieval boundary are unchanged. Unknown formats fail before retrieval.

## Output and evidence boundary

The optional `troubleshooting` object has a strict version-1 schema. Markdown
and structured results contain a conclusion plus six ordered sections: possible
causes, roles/permissions, vehicles/equipment, work rules, environment differences
and verification. The existing citation list supplies document and page references.

This is a deterministic, extractive template, not an LLM diagnosis or live
Environment Inspector. Bilingual keyword matching only routes original excerpts
into sections. It does not establish semantic relevance, document applicability
or causality. A section with no matching evidence is explicitly `evidence_gap`;
it does not receive invented configuration instructions. Every populated evidence
item contains an exact citation excerpt and its citation ID. Negation is preserved.
Evidence rejected by the existing sufficiency gate is never promoted.

`root_cause_confirmed` is always false: documents cannot establish the current
environment's state. Source text is labeled as quoted documentation, not executable
instructions. No generated remediation, automatic permission change, environment
inspection, approval bypass or WMS mutation is introduced. Existing handlers remain
responsible for validation and scope enforcement.

## Limitations and review

Keyword routing can miss synonyms or include broadly related passages. Citations
remain visible even when no section matches. Document excerpts can be truncated by
the existing citation generator; read the source before making operational decisions.
This mode does not add a new Agent workflow, persistent diagnostic session or UI;
it reuses the existing read-only MCP query, traces, reranker and multimodal output.

Development branch: `feature/diagnostic-responses`. Day 14 is included in `dev`
through PR #58. Day 15 main changes remain pending user review. Test-fixture and
format findings are tracked separately in Issue #59 / bugfix PR #60.
