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

- Implement query normalization and structured WMS filters.
- Implement dense/sparse retrieval, reciprocal-rank fusion, and rerank fallback.
- Build answers from retrieved evidence with source, page/section, and version metadata.
- Refuse unsupported answers when evidence is insufficient.
- Exit criteria: representative configuration questions return deterministic cited evidence.

## Day 5 — MCP delivery and end-to-end acceptance (2026-08-27)

- Expose query, collection listing, and document-summary MCP tools over stdio.
- Add trace records for ingestion and querying without polluting protocol stdout.
- Add an end-to-end client test and a small golden retrieval set.
- Document local setup and known MVP limits, then tag the sprint checkpoint.
- Exit criteria: a local MCP client can ingest/query the sample corpus and verify citations.
