# Same-conversation memory

The runner loads user **and assistant** turns from the scoped session repository.
History remains immutable and is not deleted when compacted. A per-session graph
checkpoint and business revision store a rolling summary, the last summarized
message sequence, recent messages, and the resolved current request. A restarted
runner resumes from that cursor. Other sessions never supply conversation memory.

Before classification/retrieval, follow-ups are rewritten into standalone requests
using recent messages and the summary. Independent new questions should remain
unchanged. Ambiguous references produce a clarification instead of a guessed query.
The user's original message remains the displayed and persisted message. Grounded
answers receive the conversational context, but prior assistant statements and
summaries are not documentary evidence: claims still require exact quotes from
the current retrieval. Requirement extraction also receives both roles and the
summary; assistant suggestions cannot establish confirmed requirements.

## Limits and overflow

- `agent.max_context_turns: 8`: maximum recent message entries, counting both roles,
  including the latest user message. This is not eight question/answer pairs.
- `agent.max_context_chars: 16000`: application conversation-context JSON character
  limit (summary, recent messages and pinned confirmed requirements). It is not
  the provider's token window and excludes fixed prompts and retrieved evidence.
- `agent.max_summary_chars: 3000`: rolling summary output character limit; smaller
  than the context limit. Both settings are validated as positive integers.
- `agent.max_tokens_per_turn`: total model usage budget for a turn, including
  summarization, follow-up resolution and answer generation. Memory calls use a
  conservative UTF-8-byte input preflight estimate plus a 1024-token output reserve;
  actual reported usage is accounted after each call. This is an application
  guard, not an exact provider tokenizer or guarantee of maximum billed output.

When count or size grows, older messages are summarized in bounded batches and
combined with the previous summary. At most four batches run in a turn. Confirmed
requirements remain separate and unchanged; the latest user message is never
truncated. The UI indicates that earlier messages were summarized. Summaries are
lossy model outputs, not a guarantee of verbatim recall; originals remain available.

If the latest input plus pinned requirements cannot fit, an older individual
message cannot fit in the summarization input, or the history exceeds the bounded
batch count, processing pauses with a context-limit explanation. Excessive older
history may require a new conversation with the relevant background. Invalid
summary/rewrite output or provider failure pauses explicitly. No retrieval occurs
with partially prepared context. Failed compaction does not advance the cursor;
retry can re-read originals. Budget/time failures likewise pause and do not loop.

The summary prompt preserves goals, negations, corrections, unresolved questions
and the distinction between user statements and assistant suggestions. It does
not replace the confirmed requirement store, grant approval, or override Workspace
filters. There is no cross-conversation preference memory or conversation-vector
retrieval. Soft deletion preserves memory; permanent conversation cleanup removes
associated revisions and checkpoints through the existing deletion workflow.

## Verification

Isolated tests cover follow-up retrieval, both message roles, rolling summaries
across restarts, session isolation, character/count limits, pinned requirements,
invalid output, ambiguous references, failed-summary retry, token budget accounting,
and rejection of historical assistant claims as unsupported documentary citations.
