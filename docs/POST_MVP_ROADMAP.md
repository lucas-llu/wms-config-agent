# WMS Configuration Agent Post-MVP Roadmap

## Planning principles

- Preserve the released evidence, revision, approval, privacy, and no-write boundaries.
- Use one short content-named feature branch per development day.
- Treat K.AI-like Workspace/Knowledge/Actions UX as productization, not a reason to weaken
  deterministic Agent gates.
- Keep real WMS execution, multi-tenant cloud hosting, and free Agent-to-Agent negotiation out of
  this roadmap.

## Eight-day productization schedule

| Day | Branch | Delivery | Acceptance |
|---|---|---|---|
| 11 | `feature/agent-capabilities` | Add `get_agent_capabilities` with contract/tool/provider/file/export/budget and safety metadata | Strict schema snapshot; public/auth semantics unambiguous; no secret/provider key values |
| 12 | `feature/workspace-scope` | Add durable Workspace with allowed collections, modules, sites, environments and policy | Sessions cannot escape workspace scope; two workspaces remain isolated |
| 13 | `feature/knowledge-catalog` | Add Knowledge catalog/read model for document version, module, scope, index health and freshness | Missing/stale scope is visible; absolute paths and private bodies remain hidden |
| 14 | `feature/action-catalog` | Add read-only Actions catalog showing annotations, permissions, approval and availability | No hidden write/environment action; capability and MCP definitions remain consistent |
| 15 | `feature/diagnostic-responses` | Add WMS troubleshooting response template: conclusion, causes, role, equipment, rules, environment, verification, citations | Unsupported claims remain assumptions/gaps; every actionable claim has evidence |
| 16 | `feature/feedback-evaluation` | Add thumbs-up/down, citation-error, incomplete-answer and regeneration-reason records | Feedback binds to workspace/session/revision/trace without storing secrets |
| 17 | `feature/agent-workbench` | Improve Agent Sessions into an end-user workbench for workspace, chat, draft, DAG, evidence and review | A user can understand current state and next action without reading raw JSON |
| 18 | `feature/product-release-gates` | Add workspace/capability/feedback golden scenarios, migration tests, docs and product release report | V1 and Agent MVP gates remain green; new product scenarios and privacy gates pass |

## Dependencies

```text
Capabilities ──► Workspace ──► Knowledge catalog ──► Actions catalog
                         └──► Diagnostic responses ──► Feedback ──► Workbench
All previous gates ───────────────────────────────────────────────► Product release
```

## Completion record

- **Day 16 — implementation ready for review (2026-09-12):** added durable,
  deduplicated feedback signals and revision-level summaries. Host Workspace and
  existing revision checks protect both read and write paths; trace IDs come from
  persisted Agent turns, with missing traces explicitly null. Fixed categories only,
  no free-text storage, automatic regeneration or approval changes. Twenty new tests
  cover persistence, historical association, isolation, rejection, annotations and
  current-turn trace binding. Full local gate: 453 passed, 2 opt-in skips; coverage
  91.00%. Ruff/format, dependencies and V1 benchmark (4/4) pass. See
  [Feedback contract](FEEDBACK_EVALUATION.md). Main feature awaits user review.

- **Day 15 — implementation ready for review (2026-09-12):** added opt-in
  `response_format: troubleshooting` to the existing knowledge query. The response
  groups exact cited excerpts into causes, roles, equipment, rules, environment and
  verification, with an explicitly unconfirmed conclusion and evidence gaps. Default
  responses and retrieval scope are unchanged. Ten new test cases cover evidence
  preservation, insufficient evidence, input rejection, schema and MCP integration.
  Local regression: 433 passed, 2 opt-in provider skips; coverage 90.88%. Ruff/format,
  dependency checks and V1 benchmark (4/4) passed. See
  [Diagnostic response contract](DIAGNOSTIC_RESPONSES.md). Merged into `dev` through
  PR #61 after local and remote gates and review comments; feature branch cleaned.

- **Day 14 — implementation ready for review (2026-09-11):** added the read-only
  `get_agent_actions` catalog from the live MCP registry, with annotations, permission
  boundaries, revision/approval prerequisites and registration availability. Capability
  discovery shares the registry; no tool handler or WMS action is executed by discovery.
  Full local regression: 423 passed, 2 opt-in provider skips; coverage 90.85%.
  Ruff/format, dependencies and V1 benchmark (4/4) passed. See
  [Actions catalog contract](ACTION_CATALOG.md). User authorized the main PR into
  `dev`; Day 12/13 merge dependencies are satisfied through PRs #54/#55.

- **Day 13 — implementation ready for review (2026-09-11):** added the privacy-safe
  `get_wms_knowledge_catalog` MCP read model with collection/module/scope/freshness filters,
  bounded pagination, document version and scope completeness, source freshness, and index
  health. Restricted Workspaces filter records before grouping and report index health as
  `unverified` without exposing global Dense/BM25 counts. Absolute and traversal source
  references are sanitized, and catalog records never include document bodies. Four new catalog
  tests cover missing scope, stale/unknown freshness, strict schemas, pagination, Workspace
  exclusion and privacy. Full regression: 420 passed, 2 opt-in provider skips, source coverage
  90.86%; V1 public benchmark 4/4, Ruff/format and dependency checks passed. See
  [Knowledge catalog contract](KNOWLEDGE_CATALOG.md). Merged through PR #55
  and included in `dev` by PR #54.

- **Day 12 — implementation ready for review (2026-09-11):** added immutable Workspace
  allowlists, host-selected scope, schema v1-to-v2 membership migration, scoped repository reads
  and writes, generated requirement/task scope checks, retrieval and catalog enforcement,
  capability metadata and Dashboard workspace selection. Existing sessions retain legacy scope
  and immutable revision contents. Eight new workspace tests cover migration, isolation,
  retrieval, MCP composition and generated scope rejection. Full regression: 416 passed,
  2 opt-in provider skips, source coverage 90.87%; V1 public benchmark 4/4, Ruff/format and
  dependency checks passed. See [Workspace contract](WORKSPACE_SCOPE.md). Merged
  into `dev` through PR #54.

- **Day 11 — completed 2026-09-05:** `get_agent_capabilities` now publishes a strict versioned
  schema covering product/contract versions, stdio/host-process authentication semantics,
  Agent/provider feature state, sanitized knowledge modules, budgets, exports, registered tool
  annotations and safety guarantees. It never exposes credential values, environment-variable
  names, private content or provider URLs. Issue #51 / PR #52 closed; 20 targeted and 408 full
  tests passed, coverage remained 90.74%, and V1/Agent release gates remained green.

## Deferred after this schedule

- Customer-authorized real-provider and real-corpus evaluation expansion.
- Broader WMS module/task template library beyond inbound appointment and receiving.
- Read-only Environment Inspector, subject to a separate permission and audit review.
- Authentication/multi-tenancy, cloud Agent server, queues, and production deployment.
- Any WMS mutation path, which requires a separate preview/approve/apply/verify safety program.
