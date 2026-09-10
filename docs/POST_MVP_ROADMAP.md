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
