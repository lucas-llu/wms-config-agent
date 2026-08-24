# LLM Provider

The project includes a provider-neutral `BaseLLM` contract and a generic OpenAI Chat Completions
compatible implementation. The committed development configuration targets OpenCode Go:

```yaml
llm:
  provider: openai_compatible
  model: ox-alpha-free
  base_url: https://opencode.ai/zen/go/v1/chat/completions
  api_key_env: WMS_LLM_API_KEY
  timeout_seconds: 60
  max_tokens: 1024
  temperature: 0
  max_retries: 2
  retry_backoff_seconds: 0.5
```

No credential is stored in YAML, `.env.example`, test output, traces, or Git. Supply the key only
to the process environment. This PowerShell pattern avoids placing the key in shell history:

```powershell
$secureKey = Read-Host "OpenCode Go API key" -AsSecureString
$env:WMS_LLM_API_KEY = [System.Net.NetworkCredential]::new("", $secureKey).Password
```

Run the opt-in live acceptance test:

```powershell
$env:WMS_LLM_INTEGRATION = "1"
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_chunk_refiner_llm.py
Remove-Item Env:WMS_LLM_INTEGRATION
Remove-Item Env:WMS_LLM_API_KEY
```

The test uses synthetic WMS text only. It validates real ChunkRefiner output and structured
MetadataEnricher JSON without sending the private PDF corpus. Because remote generation is
non-deterministic, acceptance permits a deterministic rule fallback only when the response was
explicitly rejected by an output/shape guard. Provider, authentication, and transport failures
still fail the live test.

## Enabling ingestion-time generation

Both text transforms remain local and deterministic by default:

```yaml
ingestion:
  chunk_refiner:
    use_llm: false
  metadata_enricher:
    use_llm: false
```

Changing either flag changes the preprocessing signature and reprocesses affected documents. A
full 1,593-Chunk rebuild can produce thousands of API requests. The corpus CLI therefore refuses
an LLM-enabled run unless both a document cap and a shared logical-call budget are provided.

For an explicitly authorized two-document canary:

```powershell
.\.venv\Scripts\python.exe scripts\process_corpus.py `
  --source ..\14_system_training `
  --enable-llm `
  --max-documents 2 `
  --max-llm-calls 20
```

`--enable-llm` is an explicit data-egress decision and enables both text transforms only for that
process. Prefer enabling one transform in `config/settings.yaml` when evaluating them separately.
If the budget is exhausted or a provider/output guard rejects a result, the deterministic rule
result remains usable and the Chunk is recorded in
`data/corpus/processed/llm_failures.jsonl`. Retry only those Chunks later with the same bounds:

```powershell
.\.venv\Scripts\python.exe scripts\process_corpus.py `
  --source ..\14_system_training `
  --enable-llm `
  --max-documents 2 `
  --max-llm-calls 20 `
  --retry-llm-failures
```

The output guard rejects empty, severely truncated/expanded, code-changing, or technically
inconsistent refinements. Metadata output is also rejected when it invents strong technical
identifiers. Metadata tags compare identifiers case-insensitively, but authoritative refined text
must preserve their exact spelling and case. Unicode replacement characters are rejected. Guard
details and exact sanitized failure types are written to Chunk metadata and the failure ledger;
rejected output is never persisted as the authoritative Chunk text.

The Provider retries only transient network failures, HTTP 429, and HTTP 500/502/503/504. It does
not retry authentication, configuration, or malformed-response errors. Traces record provider,
model, timing, status, and error type, but never prompts, responses, or credentials.
