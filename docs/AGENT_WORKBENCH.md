# Agent workbench

Day 17 upgrades the existing **Agent Sessions** Dashboard page. Launch the
Dashboard using the existing project instructions and select Agent Sessions.
The host's `WMS_CONFIG_PATH` and `agent.workspace_id` select the Workspace; the
page is not a multi-tenant login or a Workspace permission-switching interface.

## Workflow

1. Enable Agent in the host configuration, then submit a configuration goal.
2. Select a saved session and an explicit revision. Read the status and next step.
3. Use **对话** to see saved user/assistant turns and answer outstanding questions.
4. Use **配置草稿与依赖** for context, the task graph, parameters and configuration,
   verification and rollback steps. Missing details are labeled, not invented.
5. Use **引用证据** for cited excerpts, document/page/version, task bindings and gaps.
6. Use **审查与导出** to validate, then submit an explicit decision with a comment
   and confirmation checkbox. Only approved current revisions can be exported.
7. Use **反馈** for fixed-category feedback on the selected revision and its summary.

Exports use the existing host-configured export directory and service. This page
does not download files, execute WMS changes or run automatic regeneration.
After an operation, select the newest revision to inspect its result. The revision
selector does not silently turn a historical selection into a new approval target.

## Safety and behavior

- Rendering and form editing do not execute tools. Provider-backed MCP composition
  is lazy and happens only after explicit submission.
- Tool dispatch is allowlisted; selected session/revision cannot be overridden by
  extra action fields. Repository membership checks protect both reads and actions.
- Historical revisions disable continuation, validation, review and export. Feedback
  remains attached to that historical revision. Backend optimistic checks remain
  authoritative if another client changes the session while the page is open.
- Approval requires a comment and explicit confirmation; the backend still enforces
  validation, evidence and approval state. UI disabled states are not authorization.
- Chat shows stored user/assistant turns only up to the selected revision; it never
  exposes system/tool turns. Authorized users can see their own conversation and
  source excerpts; this is intentionally richer than the old operational read model.
- Evidence filesystem paths are hidden when absolute, drive-relative or traversal
  references. Content is rendered as text, not executable HTML. Graph identifiers
  are internal and labels escaped. No raw state JSON is required for normal use.
- Agent-disabled, empty and error states are explicit. Errors do not expose provider
  exception text, and failed operations are not automatically retried. A failure
  after persistence can still leave a completed operation; refresh before retrying.

## Delivery

Day 16 merged into `dev` through PR #64 after local and remote checks and both PR
comments. Day 17 develops on `feature/agent-workbench` and awaits user review.
Streamlit AppTest covers form behavior and page navigation; real-provider and live
browser acceptance remain environment-specific. Day 18 adds release-level scenarios.

Local validation: 32 targeted tests passed; full regression 462 passed, 2 opt-in
provider tests skipped, source coverage 90.98% against the 90% gate. Ruff/format,
dependency checks and V1 benchmark (4/4) passed. Day 17 main PR is not opened yet.
