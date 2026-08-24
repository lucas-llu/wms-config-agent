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
MetadataEnricher JSON without sending the private PDF corpus.

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
full 1,593-Chunk rebuild can produce thousands of API requests, so enable one transform at a time
and validate a small authorized sample before running the private corpus.

The Provider retries only transient network failures, HTTP 429, and HTTP 500/502/503/504. It does
not retry authentication, configuration, or malformed-response errors. Traces record provider,
model, timing, status, and error type, but never prompts, responses, or credentials.
