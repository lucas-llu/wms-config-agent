# Conversation management and chat feedback

Select a history conversation, then **删除对话** to move it into the sidebar
recycle bin. **恢复** restores the conversation and its selected current revision.
This is recoverable deletion: stored turns, immutable revisions, approvals,
checkpoints, feedback and previously exported files are retained. It is not data
erasure. No existing conversation is removed automatically.

Deletion is durable in an additive SQLite `deleted_sessions` table. Reads and
writes through the current repository reject deleted conversations until restored;
history and recycle-bin operations are Workspace scoped. Existing revisions and
schema version are preserved. Older binaries that do not know this table can show
deleted sessions again; permanent purge is outside this feature.

## Sending and waiting

Chat Enter submission queues the message before rendering, shows it immediately,
and displays an animated assistant status while the synchronous backend runs.
The composer and navigation are disabled during processing. Suggested prompts still
only prefill input. Failures retain the attempted message and an explicit failure
notice; retries are manual, with no automatic duplicate request.

The status describes processing, not hidden model reasoning. Motion is disabled
for users with reduced-motion preferences while the status text remains visible.

## Draft, evidence and approval

Atomic questions now store only validated cited sources in `answer_evidence`, with
their citation indexes, in the immutable conversation revision. They are displayed
under **本次回答的引用**. Task evidence remains in `evidence_registry`; ordinary
answers do not turn into configuration evidence or approved drafts.

Questions without a configuration plan explicitly explain why the draft/review
areas are empty. Draft validation is unavailable when there are no tasks. Existing
answers from before this change have no structured source snapshot; the panel asks
the user to re-ask, rather than inventing or retrospectively changing citations.

## Verification

Internal PR #87 resolves Issues #85/#86 after 497 full tests passed (3 explicit
live-test skips, 91.00% source coverage). A synthetic 8-second delayed browser check
verified the submitted message, status animation and disabled composer before the
answer completed, and only one user turn was persisted.

History deletion and restoration are checked with synthetic data only, including
restart persistence, unchanged revisions, blocked late writes and Workspace isolation.
No real user conversation is deleted during development or browser QA.

Final feature regression: 500 passed, 3 explicit live-test skips, source coverage
91.00%. Conversation deletion/restore plus question evidence/pending states are
tested with isolated data; the existing 8509 test page is updated after verification.
