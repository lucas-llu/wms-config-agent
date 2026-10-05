# WMS Configuration Agent MVP Release Candidate

## Scope

The candidate is a local, single-user, read-only configuration assistant. It manages durable
sessions, requirements, task DAGs, RAG evidence, deterministic conflicts and validation, human
review, revisioned drafts, JSON/Markdown export, MCP tools, audit traces, and Dashboard inspection.

## Release gates

- Six public sanitized Agent golden scenarios must pass.
- All P0 FR-AGT requirements must have deterministic or end-to-end coverage.
- Full source coverage must remain at least 90%.
- V1 retrieval benchmark, MCP E2E, Dashboard AppTest, privacy, secret scan, and V2 E2E must pass.
- Unauthorized tool calls must remain zero; evidence gaps and blocking conflicts must never reach
  review.

## Candidate result — 2026-09-05

- Full test suite: 405 passed, 2 explicit real-provider tests skipped.
- Source coverage: 90.74% (minimum 90%).
- V1 sanitized retrieval benchmark: 4/4 cases passed; every quality/latency threshold passed.
- V2 Agent golden scenarios: 6/6 passed.
- Agent metrics: all 14 thresholds passed, including invalidation, evidence-gap blocking,
  conflict detection, recovery, isolation, solution completeness, and zero unauthorized calls.
- Ruff check/format, dependency compatibility, diff hygiene, and local secret-pattern checks passed.
- The Agent real-provider acceptance was not enabled (`WMS_AGENT_LIVE` unset); this is recorded as
  an opt-in acceptance limitation, not silently treated as a passing real-provider run.

## Default feature state

`agent.enabled` remains `false`. Enabling requires an authorized, aligned local corpus, configured
provider credentials, and explicit acceptance of local session/trace/export data handling.

## Known limitations

- The sanitized planning template covers inbound appointment/receiving, not every WMS module.
- No real WMS write or environment-inspection tool is exposed.
- There is no multi-tenant authorization, cloud Agent server, or long-running job queue.
- Semantic contradictions inside prose are not a substitute for structured scope conflict rules.
- The real-provider Agent test is opt-in and is not a deterministic public CI prerequisite.
- The Dashboard provides operational inspection rather than a polished end-user workspace UI.

## Deferred product work

Workspace scope, capability discovery, Knowledge/Actions product UI, user feedback, diagnostic
answer templates, broader WMS task templates, optional providers, and production deployment are
planned after the MVP release gate.
