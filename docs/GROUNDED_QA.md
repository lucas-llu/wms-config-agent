# Grounded knowledge answers

Issue #78 replaces the workbench's raw excerpt dump with a generated conclusion
and supporting citations. Atomic questions retrieve up to five scoped snippets;
only those snippets and the question are sent to the configured LLM. This is a
change from local-only extractive question delivery. Do not enable this workflow
for documents not approved for the configured external provider.

The answer uses structured claims, source IDs and exact quotes. Unknown source IDs,
missing claims and quotes absent from the supplied evidence are rejected. These are
mechanical citation checks, not a proof of semantic entailment. Business review is
still required. Empty or invalid answers produce explicit failure/gap messages,
not fabricated instructions or an unlabelled fallback to raw excerpts.

The model must distinguish entity scope, examples from requirements and tracking
from RF scan confirmation. It must explain evidence gaps when the actual setting
is unavailable. Only cited sources appear in the answer. Image placeholder hashes
are stripped before generation. Agent citation excerpts can use up to 1800 characters
to avoid truncating the relevant paragraph; the V1 query tool remains unchanged.

Existing per-turn budget accounting applies to answer generation. A bounded single
repair attempt is allowed. No approval, export or WMS mutation occurs when answering.

## Known limitation: image-only configuration fields

The trolley example exposed an independent retrieval limitation: the slot handling
unit attributes are shown in a screenshot on page 6, while the trolley's settings
are described on page 7. The text index cannot infer image-only values. Human visual
inspection confirmed that the slot example shows Serialized=No and LPN Tracked=No;
the trolley instructions specify LPN Tracked=Yes. This human finding is not silently
injected as a general system rule or fabricated as a retrieved text quote.

Therefore generated answers may correctly request screenshot/source verification.
Reliable automated answers to image-only fields require a separately validated
OCR/vision ingestion path. Do not equate no ID tracking with skipping RF slot scans.
