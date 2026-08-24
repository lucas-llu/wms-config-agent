# Private WMS Corpus Processing

The source corpus is proprietary runtime data, not repository content. Keep it outside the
public Git repository. The generated manifest, extracted documents, chunks, images, databases,
and later vector indexes all live under `data/`, which is ignored by Git.

## Current corpus baseline

- Source: `../14_system_training`
- PDF documents: 191 unique files
- Pages: 1,458
- Business process codes: 103
- Configuration documents: 88
- Operation documents: 103
- Paired configuration/operation process codes: 88
- Day 7 enriched preprocessing: 191 succeeded, 0 failed, 1,593 chunks
- Chunks with image references: 1,115
- Logical image IDs: 3,316; content-deduplicated image files: 1,883

The manifest parser uses the stable `SWL.<domain>.<group>.<process>` code for relationships. It
also recognizes misspelled configuration suffixes such as `Configurtaion` and `Configuratoin`.

## Build the private manifest

```powershell
.\.venv\Scripts\python.exe scripts\build_corpus_manifest.py `
  --source ..\14_system_training `
  --output data\corpus\manifest.jsonl
```

The JSONL manifest contains hashes, relative paths, document relationships, WMS domain/stage,
document type, page count, size, and version placeholder. It never contains extracted PDF text.

## Run text preprocessing

```powershell
.\.venv\Scripts\python.exe scripts\process_corpus.py `
  --source ..\14_system_training
```

Outputs:

- `data/corpus/manifest.jsonl`: private source catalog
- `data/corpus/processed/documents/`: normalized Document JSON
- `data/corpus/processed/chunks/`: traceable Chunk JSONL grouped by document
- `data/corpus/processed/processing_report.json`: last-run result
- `data/db/corpus_preprocessing.db`: preprocessing-only SHA256 status
- `data/images/wms-system-training/`: content-addressed extracted images
- `data/db/image_index.db`: SQLite image ID to local-path index

Use `--force` after intentionally changing preprocessing, transform, or chunk settings. Image
extraction is controlled by `ingestion.extract_images` and is enabled for this local corpus.
Use `--no-extract-images` for a temporary text-only run. The transform chain performs
deterministic cleanup and title/summary/tag enrichment locally. Text and Vision providers use the
documented `BaseLLM`/`BaseVisionLLM` factory contracts. The configured OpenAI-compatible text
provider has passed live OpenCode Go acceptance, but both text transforms keep `use_llm: false` by
default to avoid accidental bulk API usage. See `docs/LLM_PROVIDER.md` before enabling either
transform. Vision generation remains disabled, and unprocessed image references remain traceable.

The preprocessing history includes a signature of splitter settings, transform implementations,
prompts, provider classes, and image options. A changed signature automatically reprocesses the
affected source even when its PDF hash is unchanged; `--force` remains available for an explicit
full rebuild.

## Build the retrieval indexes

The JSON artifacts are auditable staging data, not the retrieval index. Build both retrieval
indexes with:

```powershell
.\.venv\Scripts\python.exe scripts\ingest.py
```

The current offline baseline trains a corpus-specific TF-IDF + truncated-SVD (LSA) model. It
produces normalized 256-dimensional dense vectors without sending proprietary text to an API.
The embedding provider and vector store are selected through `config/settings.yaml`, so this
baseline can later be replaced by a multilingual Sentence Transformer or hosted embedding
model without changing the indexing pipeline.

Private outputs:

- `data/models/local_lsa/tfidf-svd-256.joblib`: fitted embedding model;
- `data/db/chroma/`: persistent Chroma collection `wms_config_chunks`;
- `data/db/bm25/index.json`: persistent Okapi BM25 index.

Verified Day 7 baseline on 2026-08-24:

- dense vectors in Chroma: 1,593;
- documents in BM25: 1,593;
- full indexing run: 1,593 upserted and 1,024 stale vectors removed;
- private Benchmark V1: 40/40 passed with Hit@1/3/5 and MRR@5 at 100%;
- public sanitized benchmark: 4/4 passed with no regression.

Use `--force` after intentionally changing embedding behavior or replacing the full corpus. A
forced full rebuild now removes Chroma IDs absent from the current corpus after current records
are successfully upserted.
Index freshness is determined by the embedding-model signature and each chunk's SHA256 content
hash. Metadata stored with each vector includes its process code, domain, document type, source
path, page range, summary, tags, and image references.

## Diagnostic vector query

```powershell
.\.venv\Scripts\python.exe scripts\query_vector.py `
  "How does directed putaway choose a storage location?" `
  --process-code SWL.I.11.01 `
  --top-k 3
```

The diagnostic command queries Chroma directly and supports `--domain`, `--document-type`, and
`--process-code` filters. For normal use, run the hybrid query command documented in
`docs/QUERYING.md`; it combines dense and BM25 candidates with RRF so exact WMS identifiers and
natural-language similarity complement each other.
