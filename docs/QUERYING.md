# Hybrid WMS Document Querying

Day 4 provides a local citation-first retrieval path over the private indexes built on Day 3.
The LLM remains disabled, so the command returns relevant source excerpts and page references
without inventing a configuration answer.

## Run a query

```powershell
.\.venv\Scripts\python.exe scripts\query.py `
  --query "如何配置 SWL.I.11.01 的上架库位？" `
  --top-k 5
```

Optional metadata filters:

```powershell
.\.venv\Scripts\python.exe scripts\query.py `
  --query "Appointment Creation" `
  --document-type configuration `
  --domain Inbound `
  --top-k 5
```

Use `--verbose` to display inferred filters, Chinese WMS term expansions, candidate counts, and
retrieval failures. Use `--json` for a structured response containing citations and diagnostics.

## Query flow

1. normalize whitespace and extract searchable terms;
2. infer `document_type=configuration` for configuration intent;
3. infer an explicit `SWL.*` process code and unambiguous WMS domain;
4. expand a small, deterministic Chinese/English WMS glossary;
5. run dense Chroma and sparse BM25 retrieval concurrently;
6. post-filter both paths and combine ranks with RRF;
7. limit repeated chunks from the same source document;
8. return source excerpts with process code, relative path, and page range.

The glossary is a local MVP bridge for Chinese questions over the English corpus. It is not a
general translation model. Unknown Chinese or English concepts are refused when query-specific
terms do not occur in the retrieved evidence. Zero-length or zero-valued query vectors never
reach Chroma.

## Verified private-corpus baseline

The following regression was run against 1,274 indexed chunks on 2026-08-24:

| Query set | Hit@1 | Hit@3 | Hit@5 |
|---|---:|---:|---:|
| 88 configuration document titles | 93.2% | 100.0% | 100.0% |
| 12 English/Chinese semantic questions | 91.7% | 100.0% | 100.0% |
| 103 exact process codes | 100.0% | 100.0% | 100.0% |

An unsupported `quantum portal configuration` query was also verified to return
`insufficient_evidence` rather than unrelated configuration excerpts.

## Known MVP limits

- The Chinese expansion glossary only covers common WMS terms currently represented in tests.
- RRF is the final ranking stage while `rerank.backend` is `none`; a cross-encoder is not enabled.
- Results are extractive evidence, not a generated step-by-step configuration answer.
- Evidence sufficiency is deterministic lexical validation, not an LLM faithfulness score.
