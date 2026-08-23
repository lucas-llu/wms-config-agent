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
- First text preprocessing run: 191 succeeded, 0 failed, 1,274 chunks
- Incremental verification run: 191 skipped, 0 reprocessed

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

Use `--force` after intentionally changing preprocessing or chunk settings. Image extraction is
off by default for the initial text pass; use `--extract-images` only for a targeted corpus run
until repeated logos, icons, and screenshots can be classified and deduplicated.

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

Verified baseline on 2026-08-23:

- dense vectors in Chroma: 1,274;
- documents in BM25: 1,274;
- first indexing run: 1,274 upserted;
- unchanged second run: 1,274 skipped, 0 upserted.

Use `--force` after intentionally changing embedding behavior. Index freshness is determined by
the embedding-model signature and each chunk's SHA256 content hash. Metadata stored with each
vector includes its process code, domain, document type, source path, and page range.

## Diagnostic vector query

```powershell
.\.venv\Scripts\python.exe scripts\query_vector.py `
  "How does directed putaway choose a storage location?" `
  --process-code SWL.I.11.01 `
  --top-k 3
```

The diagnostic command queries Chroma directly and supports `--domain`, `--document-type`, and
`--process-code` filters. It is not the final question-answering path. Day 4 combines dense and
BM25 candidates with RRF so exact WMS identifiers and natural-language similarity complement
each other.
