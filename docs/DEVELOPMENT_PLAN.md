# Five-Day MVP Development Plan

This sprint delivers a local, citation-first WMS/JDA MOCA configuration knowledge agent.
The scope intentionally excludes production WMS connections, configuration writes, and
undocumented data. All document fixtures must be authorized and sanitized.

## Day 1 — Foundation and stable contracts (2026-08-23)

Status: completed on 2026-08-23.

- Add the pytest and linting baseline.
- Load and validate typed YAML settings with readable field-level errors.
- Define shared `Document`, `Chunk`, and `ChunkRecord` contracts.
- Add SHA256 and SQLite WAL-based ingestion history for incremental processing.
- Exit criteria: A2, A3, C1, and C2 tests pass without external services.

## Day 2 — Local document loading and chunking (2026-08-24)

Status: completed early on 2026-08-23.

- Define loader and splitter extension contracts.
- Implement authorized PDF/Markdown/TXT loading with WMS source metadata.
- Implement deterministic recursive chunking and preserve source/page references.
- Add rules for version, module, site, and environment metadata.
- Exit criteria: a sanitized MOCA document becomes stable chunks with traceable sources.

## Day 3 — Offline indexing and ingestion pipeline (2026-08-25)

Status: completed early on 2026-08-23.

- Implement a local embedding provider and pluggable provider factories.
- Implement persistent vector records and a BM25 index.
- Compose load, split, encode, and upsert stages with progress callbacks.
- Add an `ingest` CLI and prove unchanged documents are skipped.
- Exit criteria: a fixture collection can be ingested twice, with the second run skipped.

Delivered: 1,274 private chunks were encoded as 256-dimensional local LSA vectors and
persisted to Chroma. The same chunks were added to a persistent BM25 index. A second run
skipped all 1,274 dense records, and diagnostic vector queries returned source/page metadata.

## Day 4 — Hybrid retrieval and citation-first answers (2026-08-26)

Status: completed early on 2026-08-24.

- Implement query normalization and structured WMS filters.
- Implement dense/sparse retrieval, reciprocal-rank fusion, and rerank fallback.
- Build answers from retrieved evidence with source, page/section, and version metadata.
- Refuse unsupported answers when evidence is insufficient.
- Exit criteria: representative configuration questions return deterministic cited evidence.

Delivered: concurrent dense/BM25 retrieval, rule-based Chinese WMS query expansion, inferred
configuration/process filters, RRF fusion, document diversity, safe reranker fallback, cited
extractive responses, and explicit refusal for unsupported queries. Real-corpus regression
reached Hit@3 100% on 88 configuration documents, 12 bilingual semantic questions, and Hit@1
100% on all 103 process-code lookups.

## Day 5 — MCP delivery and end-to-end acceptance (2026-08-27)

Status: completed early on 2026-08-24.

- Expose query, collection listing, and document-summary MCP tools over stdio.
- Add trace records for ingestion and querying without polluting protocol stdout.
- Add an end-to-end client test and a small golden retrieval set.
- Document local setup and known MVP limits, then tag the sprint checkpoint.
- Exit criteria: a local MCP client can ingest/query the sample corpus and verify citations.

Delivered: a newline-delimited JSON-RPC stdio server with legacy initialize and modern
stateless MCP compatibility; three read-only WMS tools; safe text/image content assembly;
query and ingestion JSONL timing traces; a sanitized golden retrieval set; local host setup
documentation; and a subprocess E2E test covering handshake, discovery, cited retrieval,
trace persistence, and protocol stdout isolation.

## Day 5.5 — Retrieval Benchmark V1 (2026-08-24)

Status: completed on 2026-08-24.

- Define a strict benchmark schema and human-labeling policy.
- Separate a committed sanitized smoke set from the ignored private real-corpus benchmark.
- Measure Hit@1/3/5, MRR@5, refusal/evidence accuracy, and P50/P95 latency.
- Freeze the current retrieval baseline and enforce regression floors from a CLI.
- Exit criteria: the 40-case private set and four-case public set run reproducibly, produce
  sanitized reports, and pass their declared regression gates.

Delivered: 40 private cases across exact-code, bilingual semantic, document-type, and refusal
categories; a SHA-256 fingerprinted baseline report; four preserved bad cases for Day 6;
schema/metric unit tests; and `scripts/run_benchmark.py` for repeatable local evaluation.

## Day 6 — Evaluation loop and retrieval optimization (2026-08-24)

Status: completed on 2026-08-24.

- Add provider-neutral evaluator contracts, a configuration factory, local threshold checks,
  and parallel composite evaluation with provider failure isolation.
- Compare candidates with a fingerprint-matched frozen baseline and report metric deltas,
  fixed cases, new failures, persistent failures, and rank changes.
- Record relevant ranks separately for Dense, BM25, raw RRF fusion, and final results.
- Use Benchmark V1 failures to improve bilingual WMS expansion, configuration intent
  detection, and trusted document-title metadata ranking.
- Exit criteria: all four known failures are fixed, no new benchmark failure is introduced,
  refusal accuracy remains 100%, and the full automated suite passes.

Delivered: all 40 private benchmark cases now pass. Hit@1/3/5 and MRR@5 reached 100%,
refusal and evidence accuracy remained 100%, and the candidate comparison reports no quality
regression. A sanitized ingest-to-benchmark E2E recall test now enforces the evaluation path.

## Day 7 — Document quality and multimodal enrichment (2026-08-24)

Status: completed on 2026-08-24.

- Add an atomic `BaseTransform` contract and deterministic chunk cleanup that preserves code.
- Generate rule-based title, summary, and retrieval tags with optional LLM fallback paths.
- Add optional Vision caption injection while preserving image references when disabled or failed.
- Store images by content hash and persist image ID mappings in SQLite WAL mode.
- Reprocess the private corpus, synchronize stale vector IDs, and enforce Benchmark V1 gates.
- Exit criteria: 191 PDFs process successfully, Chroma and BM25 counts match the enhanced Chunk
  count, all automated tests pass, and neither private nor public benchmarks regress.

Delivered: 1,593 enhanced chunks from all 191 PDFs, including 1,115 chunks with image references.
The image index tracks 3,316 logical IDs backed by 1,883 deduplicated local files. Chroma and BM25
both contain exactly 1,593 records after 1,024 stale vector IDs were removed. The 40-case private
and four-case public benchmarks remain at 100% with no regression; caption generation is safely
disabled until a Vision provider is configured.
