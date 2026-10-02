# Conversation management and chat feedback

Each history row has a **⋯** menu with **重命名** and **删除对话**. Actions target
that row, even when a different conversation is open. Deleting/restoring another
conversation preserves the open view and selected revision; deleting the open one
returns to new conversation. **恢复** reopens it when no other conversation is selected.
This is recoverable deletion: stored turns, immutable revisions, approvals,
checkpoints, feedback and previously exported files are retained. It is not data
erasure. No existing conversation is removed automatically.

Rename changes an additive display-title record, not the original goal, immutable
revision, planning requirement or approval fingerprint. Search filters sidebar rows
without clearing the open conversation. New conversation clears the search and
starts a blank composer. The selected historical revision survives sidebar reruns.

The page is a conversation workspace: **对话** contains messages and pending status;
**工作区** contains task/evidence/review summaries and the existing controlled actions.
Sending from the workspace switches to conversation; operating on a draft retains
the workspace context. Names are rendered without remote images or raw HTML.

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

User-review refinements under Issue #89 add row actions, rename and workspace
navigation. Updated full gate: 507 passed, 3 explicit live-test skips, coverage
91.01%; targeted 59 passed. Clean synthetic browser flow verified deletion,
restoration, rename, search preserving the active view and workspace evidence.
An unrelated privacy-test timestamp false positive was fixed separately via #90 / #91
without relaxing its assertions.
