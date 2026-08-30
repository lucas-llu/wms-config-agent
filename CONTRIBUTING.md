# Contributing to WMS Config Agent

Thanks for considering a contribution.

WMS Config Agent is intentionally conservative: retrieval quality, source traceability, privacy, and safe failure behavior matter more than producing an answer at any cost.

## Good contribution areas

- retrieval and reranking experiments
- benchmark and evaluation improvements
- MCP integrations
- dashboard UX and diagnostics
- generic enterprise-documentation examples
- privacy and security hardening
- documentation and onboarding

## Before you start

1. Check existing issues and pull requests for related work.
2. For larger changes, open an issue first so the approach can be discussed.
3. Do not commit private WMS/JDA manuals, processed document text, indexes, traces, secrets, `.env` files, model caches, or private benchmark reports.
4. Keep changes compatible with the repository's local-first and citation-first design unless the change is explicitly introducing an optional provider path.

## Development setup

Requirements:

- Git
- Python 3.12

```powershell
git clone https://github.com/lucas-llu/wms-config-agent.git
cd wms-config-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

On Linux/macOS, use the equivalent `.venv/bin/python` executable.

## Quality checks

Before opening a pull request, run:

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests scripts main.py
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts main.py
.\.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term-missing --cov-fail-under=90
```

For changes affecting retrieval behavior, also run the public benchmark:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_recall_benchmark.py -q
```

## Pull requests

A useful PR should explain:

- what problem it solves
- why the change is needed
- how it was tested
- whether retrieval quality, citations, privacy, or compatibility are affected

Keep PRs focused when possible. Smaller changes are easier to review and validate.

## Domain adaptations

Forks that adapt the architecture to ERP docs, SOPs, network operations, compliance manuals, support knowledge bases, or other private enterprise documentation are especially interesting.

If you discover reusable changes while adapting the project, upstream contributions are welcome.
