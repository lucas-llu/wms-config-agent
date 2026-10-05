# WMS Config Agent

**Ask questions about WMS / JDA configuration documentation and get answers backed by source citations.**

A local-first, citation-first knowledge assistant for warehouse-management and enterprise configuration documentation.

> **Private by default. No cloud required for the default workflow. If the evidence is not strong enough, the agent refuses instead of inventing an answer.**

[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![RAG](https://img.shields.io/badge/RAG-Hybrid%20Retrieval-blueviolet.svg)](#how-it-works)
[![MCP](https://img.shields.io/badge/MCP-Ready-success.svg)](#mcp-server)
[![Local First](https://img.shields.io/badge/Privacy-Local--First-brightgreen.svg)](#privacy-and-safety)

## See it in action

![WMS Config Agent dashboard showing grounded answers with source citations, retrieved evidence, diagnostics, and evaluation metrics.](docs/assets/dashboard-demo.svg)

*Illustrative dashboard preview: ask a warehouse configuration question and get a grounded answer with citations, retrieved evidence, local-first diagnostics, and evaluation metrics.*

## Why WMS Config Agent?

Warehouse configuration knowledge is often buried across hundreds or thousands of pages of manuals, implementation notes, operating procedures, and internal documentation.

Traditional search can find keywords, but it does not reliably answer questions such as:

> How should replenishment be configured for fast-moving SKUs?
>
> Which putaway rule applies to this storage profile?
>
> Where is this configuration behavior documented?

WMS Config Agent combines **vector retrieval + BM25 search + reciprocal-rank fusion** to retrieve relevant evidence and return answers tied back to their original sources.

The goal is not simply to generate an answer. The goal is to generate an answer you can **verify**.

## What you get

- **Citation-first answers** — retrieval results carry source/page evidence instead of unsupported instructions.
- **Hybrid search** — BM25 and Chroma vector retrieval are aligned and fused with reciprocal-rank fusion.
- **Local-first defaults** — the default ingestion/query path does not require an API key.
- **Evidence-aware refusal** — insufficient evidence results in a refusal rather than unsupported instructions.
- **Read-only MCP tools** — expose WMS knowledge safely to MCP-compatible desktop hosts.
- **Six-page Streamlit dashboard** — inspect data, ingestion traces, query diagnostics, lifecycle operations, and evaluation.
- **Deterministic evaluation** — run a committed public benchmark without private documents.
- **Privacy and safety controls** — local artifacts stay out of Git and risky lifecycle operations require explicit confirmation.
- **Enterprise RAG reference architecture** — adapt the same architecture beyond WMS documentation.

## How it works

```text
Authorized documents
        |
        v
Ingestion & chunking
        |
        +--> BM25 lexical index
        |
        +--> Chroma vector index
                 |
                 v
        Reciprocal Rank Fusion
                 |
                 v
          Evidence + citations
                 |
          +------+------+
          |             |
          v             v
      MCP tools      Dashboard
```

The result is a practical reference implementation for **private enterprise RAG + MCP**, using WMS/JDA documentation as the primary vertical use case.

## Use it beyond WMS

Although the project started as a WMS / JDA configuration assistant, the architecture is intentionally reusable. You can fork it and adapt the same pipeline for:

- ERP configuration documentation
- SAP implementation manuals
- internal SOPs and work instructions
- compliance and policy documentation
- network or infrastructure runbooks
- product and engineering documentation
- enterprise knowledge assistants

## Quick start

### Requirements

- Git
- Python 3.12

Commands below use PowerShell on Windows. On Linux/macOS, use the equivalent `.venv/bin/python` executable.

```powershell
git clone https://github.com/lucas-llu/wms-config-agent.git
cd wms-config-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe main.py
```

Expected final line:

```text
wms-config-agent is ready (development).
```

To reproduce the committed, fully sanitized release workflow without private documents:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_recall_benchmark.py `
  tests/e2e/test_dashboard_day9_workflow.py `
  tests/integration/test_mcp_server_e2e.py -q
```

## Ingest a document

Only ingest documents you are authorized to use.

```powershell
.\.venv\Scripts\python.exe scripts\ingest.py `
  --path C:\authorized\sanitized-manual.pdf `
  --collection sanitized-demo
```

Re-running an unchanged document is skipped. Add `--force` only when a deliberate rebuild is required.

To index existing processed JSONL chunks instead, omit `--path` or pass `--chunks PATH`.

## Query the knowledge base

```powershell
.\.venv\Scripts\python.exe scripts\query.py `
  --query "SWL.I.11.04 putaway configuration" `
  --collection sanitized-demo `
  --verbose
```

Useful filters include:

- `--domain`
- `--document-type`
- `--process-code`
- `--json`

See [docs/CORPUS_PROCESSING.md](docs/CORPUS_PROCESSING.md) and [docs/QUERYING.md](docs/QUERYING.md) for retrieval details.

## MCP server

After both indexes exist, start the newline-delimited JSON-RPC stdio server:

```powershell
.\.venv\Scripts\python.exe scripts\start_mcp_server.py
```

It exposes four read-only tools:

- `query_wms_knowledge` returns evidence excerpts with source/page citations.
- `list_wms_collections` returns privacy-safe corpus counts.
- `get_wms_document_summary` returns an extractive document summary.
- `get_wms_knowledge_catalog` returns document version, scope completeness, index health and
  freshness without exposing document bodies or absolute host paths.

The catalog supports collection/module filters, scope and freshness status filters, and bounded
pagination. See [docs/KNOWLEDGE_CATALOG.md](docs/KNOWLEDGE_CATALOG.md) for the response contract
and Workspace behavior.

Desktop MCP hosts should use absolute paths for the Python executable, script, settings, BM25 index, and processed chunks.

See [docs/MCP_SERVER.md](docs/MCP_SERVER.md) for host configuration and protocol details.

## Dashboard

Start the local-only dashboard:

```powershell
.\.venv\Scripts\python.exe scripts\start_dashboard.py
```

The dashboard binds to `127.0.0.1` and includes pages for:

- system overview and health
- read-only data browsing
- bounded PDF ingestion
- confirmation-gated cleanup
- ingestion traces
- query diagnostics
- benchmark evaluation

The Evaluation page only permits the dataset explicitly named by `evaluation.golden_test_set`. Its history stores aggregate metrics and failed case IDs, not query or document text.

## Evaluation

The committed public dataset is:

```text
tests/fixtures/golden_test_set.json
```

Run the self-contained public benchmark:

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

Use `--baseline data\evaluation\previous.json --fail-on-regression` only when the baseline has the same dataset fingerprint and case IDs.

See [docs/BENCHMARK_V1.md](docs/BENCHMARK_V1.md).

## Settings and local data

Typed configuration lives in [config/settings.yaml](config/settings.yaml).

Defaults use:

- local LSA embeddings
- Chroma
- BM25
- reciprocal-rank fusion
- no reranker
- deterministic threshold evaluation
- disabled Vision processing
- `use_llm: false` for text transforms

No API key is needed for the default ingestion/query path.

| Purpose | Default | Environment override |
|---|---|---|
| Settings | `config/settings.yaml` | `WMS_CONFIG_PATH` |
| BM25 index | `data/db/bm25` | `WMS_BM25_PATH` |
| Ingestion history | `data/db/ingestion_history.db` | `WMS_INGESTION_HISTORY_PATH` |
| Dashboard staging | `data/staging` | `WMS_STAGING_PATH` |
| Processed artifacts | `data/corpus/processed` | `WMS_PROCESSED_PATH` |
| Dashboard evaluation history | `data/evaluation/dashboard` | `WMS_EVALUATION_REPORT_ROOT` |
| Dashboard upload limit | 25 MiB | `WMS_DASHBOARD_MAX_UPLOAD_MB` |

If you explicitly enable an OpenAI-compatible text transform, set the variable named by `llm.api_key_env` (currently `WMS_LLM_API_KEY`). Read [docs/LLM_PROVIDER.md](docs/LLM_PROVIDER.md) before enabling network-backed processing.

All paths under `data/`, plus models, indexes, traces, authorized PDFs, and private benchmark reports, are local artifacts and ignored by Git.

## Quality gates

Run the same core checks used by public CI:

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests scripts main.py
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts main.py
.\.venv\Scripts\python.exe -m pytest `
  --cov=src --cov-report=term-missing --cov-fail-under=90
```

The live LLM acceptance test is skipped unless `WMS_LLM_INTEGRATION=1` is explicitly set.

GitHub Actions additionally scans the event, complete Git history, and working tree with Gitleaks, then runs the committed public benchmark gate.

## Privacy and safety

- Never commit authorized/private PDFs, processed text, indexes, model caches, traces, `.env` files, secrets, or private evaluation reports.
- Query traces contain the user's query and inferred filters; protect the local `logs/` directory.
- Agent session databases and exports contain conversation text, confirmed context, decisions,
  approvals, and configuration drafts. Keep `data/db/agent_checkpoints.db`,
  `data/db/configuration_sessions.db`, and `data/exports/` local; all remain ignored by Git.
- MCP structured citations remove absolute host paths. Dashboard trace readers remove known
  credential/body fields and bound the amount of history read.
- Dashboard file uploads accept PDFs, enforce a size limit, stage atomically, and require an
  explicit collection. Deletion requires the exact displayed confirmation phrase and coordinates
  Chroma, BM25, image, history, and allowlisted artifact cleanup.
- The product is local and single-user. It has no production WMS write path, network auth, or
  multi-tenant isolation.

## Project status

The V1 RAG workflow and V2 configuration Agent are available in the main branch.
The Agent adds multi-turn requirements, scoped retrieval, configuration planning,
evidence review, explicit approval/export, conversation memory and history management.
It remains a local single-user application; multi-user development is a separate track on `dev`.

Deferred provider work includes Ollama/hosted embeddings, cross-encoder rerankers,
Azure Vision and Ragas. See [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md)
and [docs/AGENT_DEVELOPMENT_PLAN.md](docs/AGENT_DEVELOPMENT_PLAN.md) for delivery records.

## Troubleshooting

- **`No retrieval index found`** — ingest an authorized PDF or compatible processed chunks first; both Chroma and BM25 must be non-empty and aligned.
- **Local LSA model mismatch** — after changing corpus, embedding dimensions/model, or chunking, rebuild both indexes together.
- **Lifecycle lock timeout** — stop another ingestion/cleanup process and retry. Do not delete a live lock based only on file age.
- **Windows sharing violation** — close processes holding model/index files and retry.
- **Chroma compatibility error** — install the pinned `chromadb>=1.5,<1.6` range from this project.
- **Dashboard cannot load** — check `WMS_CONFIG_PATH` and local storage overrides. Use `scripts/verify_dashboard_readonly.py` to verify management reads against an existing store.
- **Live provider test skipped** — expected for offline development. Enable it only after reading the provider/privacy instructions and setting the required key.

## Contributing

Issues, experiments, documentation improvements, retrieval ideas, MCP integrations, and domain adaptations are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and contribution guidelines.

Good contribution areas include:

- additional retrieval/reranking strategies
- generic-domain examples
- MCP host integrations
- evaluation tooling
- documentation and onboarding improvements
- privacy/security hardening

If you adapt the project to a different enterprise documentation domain, consider opening an issue or PR describing what changed and what was reusable.

## License

MIT License. See [LICENSE](LICENSE).

---

If this project is useful to you, consider giving it a ⭐ — it helps other developers working on private enterprise RAG, MCP, and WMS tooling discover the project.
## V2 configuration Agent

### Local Workspace scope

The host selects one workspace with `agent.workspace_id`. Existing sessions migrate to
`workspace:legacy`, which preserves the earlier local unrestricted scope. Restricted workspaces
require non-empty collection, module, site and environment allowlists. They are local scope
boundaries, not authenticated multi-tenant accounts.

Provision a workspace before selecting it:

```powershell
.\.venv\Scripts\python.exe scripts/create_workspace.py --id workspace:dc01 --name DC01 `
  --collections wms-dc01 --modules inbound appointment integration --sites DC01 --environments test
```

Set `agent.workspace_id: workspace:dc01` in the selected settings file and restart the MCP host
and Dashboard. A missing workspace fails startup. The administrative provisioning command is
local; chat tools cannot change their host workspace or modify workspace policy.

Session membership and policies are immutable. Create a new workspace/session when scope changes.
Database schema v2 migrates existing membership without rewriting immutable revision JSON or
fingerprints. Retain a database backup before migration; older binaries must not open a v2 store.
Unknown future schema versions are rejected.

All six session tools use the selected repository scope, including historical revisions,
approval and export. V1 query and catalog tools are also scoped. Retrieval injects singleton
allowlist values; a multi-valued dimension must be selected explicitly before retrieval. Missing
scope metadata is excluded. If a legacy V1 tool does not expose a dimension selector, use a
single-valued workspace for that query workflow. Workspace scope applies even when the Agent
itself is disabled. Existing V1 behavior remains available through `workspace:legacy`.

Capabilities report the host workspace and whether restricted scope is enforced. The Agent
Sessions page lists only sessions belonging to the selected workspace.

The opt-in V2 Agent manages a durable configuration workflow on top of the V1 citation-first RAG
core. Set `agent.enabled: true` only in an authorized local environment with aligned Chroma and
BM25 indexes and a configured text LLM. The default remains `false` until a real provider and
customer-authorized corpus complete acceptance.

Six session MCP tools expose the workflow:

- `start_configuration_session` — create a session and collect missing requirements;
- `continue_configuration_session` — resume an interrupt using `session_id` and
  `expected_revision`;
- `get_configuration_session` — inspect the current or an immutable historical revision;
- `validate_configuration_draft` — rerun deterministic DAG/evidence/conflict gates;
- `review_configuration_draft` — explicitly revise, reject, or approve a review-ready revision;
- `export_configuration_solution` — idempotently export an approved JSON or Markdown solution.

When the Agent is enabled, `get_agent_capabilities` provides a strict, read-only discovery
contract for product/contract versions, stdio authentication semantics, feature flags, provider
availability, supported ingestion types/modules, budgets, exports, tools and safety guarantees.
It returns only a credential-availability boolean—never keys, values, private content or internal
provider URLs.

Every mutation uses optimistic revision protection. Evidence gaps and conflicts pause instead of
being guessed away; approval is explicit and does not authorize execution in a WMS environment.
Interrupted sessions resume from the configured SQLite checkpoint after process restart.

Run the deterministic Agent release gate with no private corpus or provider credentials:

```powershell
.\.venv\Scripts\python.exe scripts\run_agent_release.py --enforce-thresholds
```

The optional real-provider intent acceptance remains disabled unless `WMS_AGENT_LIVE=1` and the
configured provider key is present.

## MVP status

V1 Days 1–10 and V2 Agent Days 1–10 are implemented on `dev`: offline ingestion, hybrid retrieval,
cited MCP delivery, document enrichment and lifecycle hardening, seven-page Dashboard, deterministic
evaluation UI, contract coverage, and sanitized release acceptance. Ollama/hosted embeddings,
cross-encoder or LLM rerankers, Azure Vision, and Ragas remain explicitly deferred provider work.

The detailed delivery record is [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md).

## V2 multi-agent architecture

The read-only [Actions catalog](docs/ACTION_CATALOG.md) exposes registered MCP
tools and their execution prerequisites without running them.

For evidence-grouped troubleshooting, call `query_wms_knowledge` with
`response_format: "troubleshooting"`; see the
[diagnostic response contract](docs/DIAGNOSTIC_RESPONSES.md). Default queries are unchanged.

Agent-enabled hosts also expose [revision-bound feedback](docs/FEEDBACK_EVALUATION.md)
recording and summaries, without free-text storage or automatic regeneration.

The **Agent Sessions** page now provides a [configuration workbench](docs/AGENT_WORKBENCH.md)
for conversation, revisioned drafts, task dependencies, evidence, review and feedback.

Run `python scripts/run_product_release.py` for executed offline product gates.
See the [Day 18 release report](docs/PRODUCT_RELEASE_REPORT.md) for evidence and unrun acceptance.

The citation-first RAG core now includes a stateful configuration assistant. It clarifies a
business goal over multiple turns, decomposes it into dependent configuration
tasks, gathers evidence for each task, surfaces version or scope conflicts, validates a versioned
draft, and require explicit human approval before exporting a configuration solution. V2 remains
read-only with respect to real WMS environments.

The formal requirements are in [DEV_SPEC.md](DEV_SPEC.md),
and the design review, technology choices, and ten-day implementation plan are in
[docs/AGENT_DEVELOPMENT_PLAN.md](docs/AGENT_DEVELOPMENT_PLAN.md).

Day 1 includes a repeatable runtime compatibility probe:

```powershell
.\.venv\Scripts\python.exe scripts\verify_agent_runtime.py
```
