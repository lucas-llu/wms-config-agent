# WMS Config Agent

Local, citation-first retrieval agent for authorized WMS/JDA MOCA configuration documents. It
ingests PDFs into aligned Chroma and BM25 indexes, exposes read-only MCP tools, and provides a
six-page Streamlit Dashboard for data operations, traces, and evaluation.

The MVP does **not** connect to a WMS, change configuration, or infer unsupported instructions.
When evidence is insufficient it refuses instead of inventing an answer.

## Quick start

Prerequisites: Git and Python 3.12. Commands below use PowerShell on Windows; on Linux/macOS use
the equivalent `.venv/bin/python` executable.

```powershell
git clone ssh://git@ssh.github.com:443/lucas-llu/wms-config-agent.git
cd wms-config-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe main.py
```

Expected final line: `wms-config-agent is ready (development).`

To reproduce the committed, fully sanitized release workflow without private documents:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_recall_benchmark.py `
  tests/e2e/test_dashboard_day9_workflow.py `
  tests/integration/test_mcp_server_e2e.py -q
```

## Settings and local data

The typed configuration is [config/settings.yaml](config/settings.yaml). Defaults use local LSA
embeddings, Chroma, BM25, reciprocal-rank fusion, no reranker, deterministic threshold evaluation,
and disabled Vision processing. Text transforms also set `use_llm: false`, so no API key is needed
for the default ingestion/query path.

Important local paths and overrides:

| Purpose | Default | Environment override |
|---|---|---|
| Settings | `config/settings.yaml` | `WMS_CONFIG_PATH` |
| BM25 index | `data/db/bm25` | `WMS_BM25_PATH` |
| Ingestion history | `data/db/ingestion_history.db` | `WMS_INGESTION_HISTORY_PATH` |
| Dashboard staging | `data/staging` | `WMS_STAGING_PATH` |
| Processed artifacts | `data/corpus/processed` | `WMS_PROCESSED_PATH` |
| Dashboard evaluation history | `data/evaluation/dashboard` | `WMS_EVALUATION_REPORT_ROOT` |
| Dashboard upload limit | 25 MiB | `WMS_DASHBOARD_MAX_UPLOAD_MB` |

If you explicitly enable an OpenAI-compatible text transform, set the variable named by
`llm.api_key_env` (currently `WMS_LLM_API_KEY`). See
[docs/LLM_PROVIDER.md](docs/LLM_PROVIDER.md) before enabling network-backed processing.

All paths under `data/`, plus models, indexes, traces, authorized PDFs, and private benchmark
reports, are local artifacts and are ignored by Git.

## Ingest and query

Only ingest documents you are authorized to use. One sanitized PDF can be processed and indexed
through the complete pipeline with:

```powershell
.\.venv\Scripts\python.exe scripts\ingest.py `
  --path C:\authorized\sanitized-manual.pdf `
  --collection sanitized-demo
```

Re-running an unchanged document is skipped; add `--force` only when a deliberate rebuild is
required. To index existing processed JSONL chunks, omit `--path` or pass `--chunks PATH`.

Query the aligned local indexes and return cited evidence:

```powershell
.\.venv\Scripts\python.exe scripts\query.py `
  --query "SWL.I.11.04 putaway configuration" `
  --collection sanitized-demo `
  --verbose
```

Useful filters are `--domain`, `--document-type`, and `--process-code`. Add `--json` for structured
output. Details of corpus preparation and retrieval behavior are in
[docs/CORPUS_PROCESSING.md](docs/CORPUS_PROCESSING.md) and
[docs/QUERYING.md](docs/QUERYING.md).

## MCP server

After both indexes exist, start the newline-delimited JSON-RPC stdio server:

```powershell
.\.venv\Scripts\python.exe scripts\start_mcp_server.py
```

It exposes three read-only tools:

- `query_wms_knowledge` returns evidence excerpts with source/page citations.
- `list_wms_collections` returns privacy-safe corpus counts.
- `get_wms_document_summary` returns an extractive document summary.

Desktop MCP hosts should use absolute paths for the Python executable, script, settings, BM25
index, and processed chunks. A complete host configuration and protocol notes are in
[docs/MCP_SERVER.md](docs/MCP_SERVER.md).

## Dashboard

Start the local-only Dashboard (bound to `127.0.0.1`):

```powershell
.\.venv\Scripts\python.exe scripts\start_dashboard.py
```

The six pages provide overview health, a read-only data browser, bounded PDF ingestion and
confirmation-gated cleanup, ingestion traces, query diagnostics, and benchmark evaluation. The
Evaluation page only permits the dataset explicitly named by `evaluation.golden_test_set`; its
history displays aggregate metrics and failed case IDs, not query or document text.

## Evaluation

The committed public dataset is `tests/fixtures/golden_test_set.json`. The self-contained public
gate builds a sanitized temporary index and needs no private corpus:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_recall_benchmark.py -q
```

To evaluate an already indexed compatible corpus and persist a report:

```powershell
.\.venv\Scripts\python.exe scripts\run_benchmark.py `
  --dataset tests\fixtures\golden_test_set.json `
  --output data\evaluation\public-candidate.json `
  --enforce-thresholds
```

Add `--baseline data\evaluation\previous.json --fail-on-regression` only when the baseline has the
same dataset fingerprint and case IDs. Reports written by the CLI stay ignored because they may
contain sanitized queries and source-relative identifiers; Dashboard history is a narrower
privacy-safe schema. See [docs/BENCHMARK_V1.md](docs/BENCHMARK_V1.md).

## Quality and security gates

Run the same core gates used by public CI:

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests scripts main.py
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts main.py
.\.venv\Scripts\python.exe -m pytest `
  --cov=src --cov-report=term-missing --cov-fail-under=90
```

The live LLM acceptance test is skipped unless `WMS_LLM_INTEGRATION=1` is explicitly set. GitHub
Actions additionally scans the event, complete Git history, and working tree with Gitleaks, then
runs the committed public benchmark gate.

## Privacy and safety

- Never commit authorized/private PDFs, processed text, indexes, model caches, traces, `.env`
  files, secrets, or private evaluation reports.
- Query traces contain the user's query and inferred filters; protect the local `logs/` directory.
- MCP structured citations remove absolute host paths. Dashboard trace readers remove known
  credential/body fields and bound the amount of history read.
- Dashboard file uploads accept PDFs, enforce a size limit, stage atomically, and require an
  explicit collection. Deletion requires the exact displayed confirmation phrase and coordinates
  Chroma, BM25, image, history, and allowlisted artifact cleanup.
- The product is local and single-user. It has no production WMS write path, network auth, or
  multi-tenant isolation.

## Troubleshooting

- **`No retrieval index found`** — ingest an authorized PDF or compatible processed chunks first;
  both Chroma and BM25 must be non-empty and aligned.
- **Local LSA model mismatch** — after changing the corpus, embedding dimensions/model, or chunking,
  rebuild both indexes together. Do not mix a stale model with a newer BM25 index.
- **Lifecycle lock timeout** — stop another ingestion/cleanup process and retry. Do not delete an
  active or merely old-looking lock; live owners are intentionally not reclaimed by file age.
- **Windows sharing violation** — close processes holding model/index files and retry. Atomic file
  replacement uses bounded exponential backoff only for transient Windows sharing/permission
  errors and fails fast for other errors.
- **Chroma compatibility error** — install the pinned `chromadb>=1.5,<1.6` range from this project;
  the strict Dashboard metadata reader validates the expected 1.5.x schema.
- **Dashboard cannot load** — check `WMS_CONFIG_PATH` and local storage overrides. Use
  `scripts/verify_dashboard_readonly.py` to verify management reads against an existing store.
- **Live provider test skipped** — this is expected for offline development. Enable it only after
  reading the provider/privacy instructions and setting the required key.

## MVP status

Days 1–10 are implemented on `develop`: offline ingestion, hybrid retrieval, cited MCP delivery,
document enrichment and lifecycle hardening, six-page operations/trace Dashboard, deterministic
evaluation UI, contract coverage, and sanitized release acceptance. Ollama/hosted embeddings,
cross-encoder or LLM rerankers, Azure Vision, and Ragas remain explicitly deferred provider work.

The detailed delivery record is [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md).
