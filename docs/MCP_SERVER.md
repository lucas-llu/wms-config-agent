# Local MCP Server

The Day 5 server exposes the indexed WMS corpus to MCP hosts over stdio. It is read-only:
it can search evidence, list collections, and summarize documents, but it cannot connect to
or modify a WMS environment.

## Prerequisites

Create the environment and build both indexes before starting the server:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe scripts\process_corpus.py
.\.venv\Scripts\python.exe scripts\ingest.py
```

Raw documents, processed chunks, indexes, models, and traces are local artifacts under
ignored directories. Do not add authorized WMS documents or generated indexes to Git.

## Start and smoke-test

```powershell
.\.venv\Scripts\python.exe scripts\start_mcp_server.py
```

The process reads one UTF-8 JSON-RPC message per stdin line and writes one response per
stdout line. Logs are written to stderr so they cannot corrupt the protocol stream.

Available tools:

- `query_wms_knowledge`: hybrid Dense/BM25 retrieval with page-aware citations.
- `list_wms_collections`: collection, document, chunk, and domain counts.
- `get_wms_document_summary`: extractive summary by document ID, source, or process code.

## MCP host configuration

Use absolute paths because desktop MCP hosts may start the command from another directory.
Replace the example repository path with the local checkout path.

```json
{
  "mcpServers": {
    "wms-config-agent": {
      "command": "D:\\ai_dev\\jda-moca-config-agent\\.venv\\Scripts\\python.exe",
      "args": [
        "D:\\ai_dev\\jda-moca-config-agent\\scripts\\start_mcp_server.py",
        "--settings",
        "D:\\ai_dev\\jda-moca-config-agent\\config\\settings.yaml",
        "--bm25-path",
        "D:\\ai_dev\\jda-moca-config-agent\\data\\db\\bm25",
        "--chunks",
        "D:\\ai_dev\\jda-moca-config-agent\\data\\corpus\\processed\\chunks"
      ]
    }
  }
}
```

The server supports the established `initialize` lifecycle used by 2025 MCP clients and the
stateless `server/discover` flow introduced by the 2026-07-28 protocol revision.

## Tracing and privacy

When `observability.enabled` is true, query and ingestion timing records are appended to the
configured JSONL trace file. Query traces include the user query and inferred filters, so the
trace directory must be treated as sensitive local data. MCP responses remove absolute host
filesystem paths while retaining source-relative paths and page citations.

## Known MVP limits

- Answers are extractive while `llm.provider` is `disabled`; the server does not synthesize
  configuration instructions beyond the retrieved evidence.
- The bundled local LSA embedding is corpus-dependent. Re-run ingestion after the corpus or
  embedding configuration changes.
- The server is intended for a single local user over stdio. It does not provide network
  authentication, multi-tenancy, or production WMS access.
- Image blocks are returned only for existing files under explicitly allowed image roots.
