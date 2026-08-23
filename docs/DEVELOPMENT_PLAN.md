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
