# Revision-bound feedback

Day 16 adds two MCP tools when `agent.enabled` is true:

- `record_configuration_feedback`: local, non-destructive, idempotent signal storage.
- `get_configuration_feedback_summary`: read-only counts for one session revision.

Both require `session_id` and an explicit positive `revision`. Recording also
requires `kind`: `thumbs_up`, `thumbs_down`, `citation_error`, `incomplete_answer`
or `regeneration`. Only regeneration accepts a nonempty `reason`, selected from
`citation_error`, `incomplete_answer`, `unclear_answer`, `changed_requirement`.

```json
{"session_id": "session:example", "revision": 2, "kind": "regeneration", "reason": "unclear_answer"}
```

## Binding and privacy

The host-selected Workspace is authoritative. The repository verifies membership
and the existence of the requested immutable revision before reading or writing.
Historical revisions are valid feedback targets; current revision is not silently
substituted. Workspace and trace overrides, comments and arbitrary fields are rejected.

The trace ID comes from the stored revision, not the caller. New completed Agent
turns persist their current trace ID. Historical/untraced turns report null; they
are not fabricated or relabeled. Only the system trace format (32 lowercase hex
characters) is copied. Validation/review revisions may inherit the originating
Agent turn's trace; this is not a claim of a separate trace for those operations.

Feedback stores identifiers, fixed categories, a generated ID and timestamp only.
It does not copy prompts, evidence bodies, credentials or arbitrary reason text.
Existing session/trace storage policies are unchanged. This is local Workspace
isolation, not authenticated multi-user voting.

## Persistence and evaluation semantics

An additive `feedback_signals` table is created in the existing session SQLite
database, with a foreign key to the immutable revision and a uniqueness constraint
on Workspace/session/revision/kind/reason. No existing revision is rewritten and
the session schema version remains unchanged. Backup the database as usual; rollback
to Day 15 leaves an unused table without deleting feedback.

Repeated identical signals return the existing record; this also handles concurrent
retries through the database constraint. Different categories can coexist. Summary
counts are deduplicated signals, not users, approval decisions, accuracy metrics or
statistical quality scores. A regeneration reason records intent only: it does not
run an Agent, modify the draft, invalidate approval or trigger an export.

Standalone V1 queries without a durable session revision are not feedback targets
in this iteration. End-user feedback buttons and visualization belong to Day 17;
broader evaluation and migration gates remain Day 18 work.

## Delivery

Day 15 merged through PR #61. Day 16 develops on `feature/feedback-evaluation`
and awaits user review. Test-runner and lint findings follow Issue #62 / PR #63.

Local verification: 453 passed, 2 opt-in real-provider tests skipped, source
coverage 91.00%. Ruff/format, dependency check and V1 benchmark (4/4) passed.
