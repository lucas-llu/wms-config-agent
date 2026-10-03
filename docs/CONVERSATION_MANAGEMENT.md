# Conversation management and chat feedback

Each history row has a **⋯** menu with **重命名** and **删除对话**. Actions target
that row, even when a different conversation is open. Deleting/restoring another
conversation preserves the open view and selected revision; deleting the open one
returns to new conversation. **恢复** reopens it when no other conversation is selected.
This is recoverable deletion: stored turns, immutable revisions, approvals,
checkpoints, feedback and previously exported files are retained. It is not data
erasure. No existing conversation is removed automatically.

The compact icon menu closes before opening a rename or delete dialog. Rename has
aligned cancel/save actions; delete displays the target title and requires explicit
confirmation. Cancel/dismiss makes no data change. Only the destructive confirmation
is emphasized in red; each conversation retains its own row actions.

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
deleted sessions again. Permanent removal is a separate, explicitly confirmed action.

## Recycle-bin cleanup

The bin supports individual checkboxes, select/deselect all, **删除所选** and
**清空回收站**. Each destructive action confirms the count and conversation titles;
cancel/dismiss preserves every record. Emptying targets the snapshot shown when the
dialog was opened, so newly trashed conversations are not silently included.

All targets must still be deleted, belong to the current Workspace and match their
displayed revisions. A missing, restored, changed or foreign target rejects the
entire selection. The list used for clearing is not capped at 100 records. The open
conversation and selected revision remain unchanged; an empty bin has an explicit
empty state. Controls are disabled while an answer is being processed.

Purging removes session/title/tombstone records and associated turns, revisions,
decisions, approvals, export metadata and feedback. The standard workbench host also
passes `checkpoint_path` to `WorkbenchService`, allowing the corresponding SQLite
LangGraph checkpoint/writes rows to be removed in the same attached-database SQL
transaction. Ordinary SQL errors roll back both stores. This does not promise
cross-database crash atomicity in WAL mode. Hosts with a graph store must supply its
configured path. Existing exported files, backups and trace logs are retained; this
is conversation-record management rather than secure erasure of every artifact.

The current redesign is preserved. Chat spacing is increased and the composer owns
one outer focus border, with matching inner corners and a visible keyboard focus.
The sidebar's **对话轮次** selector keeps the existing history-selection behavior.
The sidebar can be resized using its right edge. Restore actions retain a fixed
action width, and the cache shortcut hint sits below its button so labels remain
horizontal even in a narrow sidebar. Keyboard shortcut binding is unchanged.
Recycle-bin titles use a single ellipsized line. The conversation action menu is
compact, and management dialogs use a 420px maximum width that fits narrow screens.
The brand area has a distinct gap before New conversation, with consistent spacing
between top-level sidebar elements. These are presentation changes; selection and
confirmation behavior remains the same.

## Sending and waiting

Same-conversation follow-ups use recent messages from both sides and a rolling
summary of older history. The UI identifies summarized history; original messages
remain available. See `CONVERSATION_MEMORY.md` for context limits, failure behavior
and the distinction between conversational context and documentary evidence.

Chat Enter submission queues the message before rendering, shows it immediately,
and displays an animated assistant status while the synchronous backend runs.
The composer and navigation are disabled during processing. Suggested prompts still
only prefill input. Failures retain the attempted message and an explicit failure
notice; retries are manual, with no automatic duplicate request.

The status describes processing, not hidden model reasoning. Motion is disabled
for users with reduced-motion preferences while the status text remains visible.

## Conversation language and clarification completion

Chinese and English conversations default to the user's question language, even
when source documents are English. An explicit request (e.g. “用英文回答”) overrides
that default. Short follow-ups such as `2024.1`, `DC01` or `test` inherit the current
language. Technical identifiers and original evidence quotes are not translated.
QA conclusions/gaps and plan descriptions receive an explicit language instruction;
wrong-language QA prose and wholly wrong-language plan titles/goals are rejected
within the existing bounded structured-output repair budget, never silently shown.

Every completed configuration turn persists an assistant reply: unresolved questions,
planning outcome, blocked validation, or readiness for human review. “需要补充的信息”
shows only current unanswered fields in a clarification pause; it is hidden while
processing a submitted answer and removed when requirements are complete. Earlier
clarification messages stay in history and historical revisions remain immutable.

Validation-blocked replies re-enter requirement extraction, replanning, evidence
retrieval and validation. Changed baselines still invalidate old tasks; a reply does
not bypass that safety check, approve a draft or execute WMS changes. Budget/provider
pauses without a graph interrupt restart against the saved baseline on a manual
retry instead of silently replaying the previous result.

## Copy and cache maintenance shortcuts

The workspace uses Streamlit viewer toolbar mode to disable the framework's legacy
developer cache shortcut (`C`). `Ctrl+C` / `Cmd+C` remains a normal browser copy
operation; ordinary `C` typing does not open cache maintenance. This does not change
terminal `Ctrl+C`, which still stops a foreground server.

`.streamlit/config.toml` sets viewer mode before the first browser session; a render-
time setting alone is too late for the initial configuration message. Launch from
the repository root so this file is loaded. External launchers can explicitly pass
`--client.toolbarMode viewer` instead.

Use **维护工具 → 清理应用缓存** or `Ctrl+Alt+Shift+K` (Mac:
`Cmd+Option+Shift+K`) to open the app-owned confirmation. Only explicit confirmation
clears `st.cache_data` and `st.cache_resource`; opening, cancelling or dismissing it
does not clear anything. Resource initialization may run again afterward. Persisted
conversations, corpus indexes and exports are not deleted. The shortcut is disabled
while processing or another management dialog is open, and in read-only Agent mode.
This is local-instance maintenance, not a per-workspace data-erasure operation.
It is enabled only when `server.address` explicitly binds the instance to
`127.0.0.1`, `::1` or `localhost`. Unspecified/all-interface/remote-capable bindings
and read-only hosts cannot perform it, even with a stale dialog state. Viewer mode
alone is not authorization; remotely deployed workbenches do not gain this global
maintenance operation.

The native button shortcut requires Streamlit 1.62 or newer, now reflected in the
declared dependency minimum. No JavaScript shortcut interceptor or dependency-source
patch is used.

## Draft, evidence and approval

Atomic questions now store only validated cited sources in `answer_evidence`, with
their citation indexes, in the immutable conversation revision and assistant-turn
metadata. Every answer displays a default-collapsed **查看证据** panel below its
conclusion; source quotes and full retrieved fragments are not printed inline.
The same sources remain under **本次回答的引用** in the workspace. Pictures resolve
and load only when their panel is opened, with unavailable-image notices and no
raw image-ID placeholders. See `GROUNDED_QA.md` for image scope and safety limits.
Task evidence remains in `evidence_registry`; ordinary
answers do not turn into configuration evidence or approved drafts.

Questions without a configuration plan explicitly explain why the draft/review
areas are empty. Draft validation is unavailable when there are no tasks. Existing
answers with an older structured snapshot use their own immutable revision, never
the newest evidence. Older inline bibliographies are folded for display without
rewriting stored turns. If no structured snapshot exists, only the old bibliography
text is shown; citations and image metadata are never invented.

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

Issue #93 clarification/language refinements: 535 passed, 3 opt-in live-test skips,
91.18% source coverage; lint and format checks pass. Offline release suites all pass
(V1: 1, Agent MVP: 1, product: 68). A freshly restarted synthetic browser verifies
menu closure, rename, delete cancellation/confirmation/restore, preservation of the
other active conversation, immediate message/pending status, resolved-question
removal, persisted configuration reply and workspace evidence. Separate real-provider
checks with synthetic English evidence pass for Chinese, English and an explicit
English override, all without repair retries. No real user history is changed by QA.

Issue #95 cache/copy refinement: 543 passed, 3 explicit live-test skips, 91.18%
source coverage; workbench suite 30 cases passed in the full run. Lint/format and offline release
gates (V1: 1 / Agent MVP: 1 / product: 76) pass. A cold-start synthetic Chrome check
verifies the native copy event (without overwriting the system clipboard), ordinary
`C` and input copy without a dialog, the replacement shortcut from a collapsed
maintenance section, cancel, explicit clearing and preservation of unsent input and history.
No real service cache is cleared during QA. Restart an already-running server and
refresh its page after installing this startup-configuration change.
