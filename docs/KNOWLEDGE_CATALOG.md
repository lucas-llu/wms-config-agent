# Day 13 Knowledge catalog contract

The Knowledge catalog is a read-only, privacy-safe projection of the local processed corpus.
It answers catalog questions without returning chunk bodies, private excerpts, or absolute host
paths.

## Tool

`get_wms_knowledge_catalog` is an MCP read-only tool with bounded pagination and optional
filters:

- `collection`
- `module`
- `scope_status`: `complete` or `incomplete`
- `freshness_status`: `fresh`, `stale`, or `unknown`
- `limit`: `1..200`
- `offset`: `0..10000`

The response contains:

- `workspace`: selected Workspace and enforcement mode
- `summary`: document, collection, scope, and freshness counts
- `index_health`: Dense/BM25/processed counts, alignment, and check time
- `documents`: metadata records with document version, module, collection, site, environment,
  page count, chunk count, scope completeness, and freshness
- `pagination`: offset, limit, returned count, and total count

## Scope completeness

The catalog treats blank, `unspecified`, `unknown`, `none`, and `n/a` values as missing for
version, module, site, and environment. Missing fields are listed explicitly under
`documents[].scope.missing_fields`, and the document is marked `incomplete`. This makes legacy or
partially enriched corpus entries visible instead of silently presenting them as fully scoped.

Workspace filtering happens before document grouping. A restricted Workspace therefore only sees
documents permitted by its collection, module, site, and environment allowlists.

## Freshness

Freshness uses the existing ingestion history record for each document hash:

- `fresh`: a successful ingestion record exists and the source file is not newer than it
- `stale`: the source file is newer than the successful ingestion record
- `unknown`: no successful record exists, the source is unavailable, or the timestamp is invalid

Freshness returns a fixed reason category and timestamps only. It never returns the source path.

## Index health

For the legacy Workspace, the catalog compares the processed chunk count with the shared Dense
and BM25 index counts. The status is `healthy` when all three counts match, `degraded` when they
differ, and `unavailable` when an index count cannot be read.

For a restricted Workspace, global index counts would disclose out-of-scope corpus size. The
catalog therefore reports `unverified` and leaves Dense/BM25 counts null while still reporting the
scoped processed-chunk count.

## Privacy

- Document bodies and summaries are never included in catalog records.
- Absolute POSIX, Windows, drive-relative, UNC, and traversal source references are reduced to a
  safe name or relative path.
- Workspace-filtered documents and counts are calculated before the catalog response is built.
- The tool is annotated read-only, idempotent, non-destructive, and closed-world.

## Verification

`tests/unit/test_knowledge_catalog.py` covers privacy filtering, missing scope, stale and unknown
freshness, strict tool schema validation, pagination, Workspace exclusion, and global index-count
non-disclosure. `tests/integration/test_mcp_server_e2e.py` verifies the tool is registered by the
stdio server.
