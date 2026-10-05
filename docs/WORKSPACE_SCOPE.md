# Day 12 Workspace contract

Workspaces are immutable local policies over collections, modules, sites and environments.
An empty allowlist is rejected for a new workspace. The reserved `workspace:legacy` policy
exists solely for backward compatibility and has unrestricted knowledge scope. Even legacy
repository instances cannot read sessions belonging to another workspace.

## Enforcement

- The host selects `agent.workspace_id`; callers supply session IDs, not policy overrides.
- Session creation stores membership in the same transaction as revision 1.
- Every session lookup, including an explicit historical revision, is scope checked.
- Revision and approval transitions reject membership changes and out-of-scope context,
  tasks or evidence before committing.
- Requirement/Planning nodes validate generated scope before downstream retrieval.
- Both Agent and V1 retrieval inject policy filters; returned metadata is rechecked.
- Catalog summaries and counts are calculated only from permitted chunks.
- Workspace creation is a local administrative operation; policy updates, transfers and
  deletion are intentionally unsupported in this iteration.

## Migration

The session database moves from user_version 1 to 2. The migration runs under BEGIN IMMEDIATE,
adds workspaces and membership, and assigns old sessions to workspace:legacy. Triggers enforce
valid membership on insert and reject rebinding and policy mutation. Revision JSON/fingerprints
are not rewritten. Reopening v2 is idempotent; future versions fail closed. This is not a
multi-tenant authorization system: operating-system access to database files remains trusted.

## Verification

`tests/unit/test_workspace_scope.py` covers persistence, migration, revision immutability,
cross-workspace access and policy validation. `tests/integration/test_workspace_workflow.py`
covers generated scope rejection, retrieval filtering, catalog scope, MCP host selection and
cross-workspace export rejection. Existing tests retain the legacy defaults.
