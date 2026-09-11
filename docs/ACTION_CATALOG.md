# Actions catalog

Day 14 adds `get_agent_actions`, a read-only MCP discovery tool accepting `{}` only.
It is registered in both V1 and Agent-enabled processes. Agent-only session tools
appear only when actually registered; disabled or deferred tools are not advertised.

## Contract

The strict version-1 output contains `workspace_id` and a name-sorted `actions` array.
Each entry exposes the registered name, title and MCP annotations, plus:

- `availability: registered`: discovery, not provider health or session readiness.
- `permission_boundary: host_process_and_workspace`: host access and existing
  Workspace enforcement, not a new authentication or authorization grant.
- `effect`: `read_only` or `local_state_change` for the current registered tools.
- `prerequisites`: relevant revision, session and review requirements.
- `requires_approved_revision`: true for solution export.

Use `tools/list` for the full input schemas. Catalog prerequisites are guidance;
the existing handlers remain authoritative for scope, revision, state and approval
validation. Discovery neither executes handlers nor reads session contents. No
Environment Inspector or real WMS write action is introduced.

The catalog reads the live registry. In the application, `get_agent_capabilities`
uses that same registry, keeping names, titles and annotations consistent with
`tools/list`, including the discovery tools themselves.

## Verification and review

Three new unit tests cover registry/capability consistency, live registration,
non-execution, unknown input rejection and strict output-schema validation. The
V1 protocol integration expectation includes the new discovery tool.

Local verification on 2026-09-11: 423 passed, 2 opt-in real-provider tests skipped;
source coverage 90.85% against a 90% gate. Ruff lint/format and dependency checks
passed; V1 sanitized benchmark passed 4/4. These results do not establish real
provider availability or production WMS correctness.

Development branch: `feature/action-catalog`, originally based on Day 13
`feature/knowledge-catalog`. Day 12/13 are now included in `dev` through PRs
#54/#55. The user has authorized the Day 14 PR into `dev`, subject to final
checks and a review comment before merge. Formatting Issue #56 was resolved
separately by merged bugfix PR #57.
