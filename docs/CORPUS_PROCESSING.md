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

## Boundary with Day 3 indexing

These JSON artifacts are auditable staging data, not the retrieval index. Day 3 continues with:

1. dense embedding generation;
2. Chroma vector persistence;
3. BM25 sparse index persistence;
4. a complete `ingest` pipeline and CLI;
5. index-aware incremental status in `data/db/ingestion_history.db`.

Keeping preprocessing and indexing history in separate SQLite files prevents a text-only run
from being mistaken for a fully indexed document.
