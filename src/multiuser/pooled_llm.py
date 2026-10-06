"""One bounded keep-alive HTTP pool per shared execution process."""

import json

import httpx

from libs.llm.openai_compatible_llm import LLMProviderError, OpenAICompatibleLLM


class PooledLLM(OpenAICompatibleLLM):
    def __init__(self, settings, *, client=None):
        self.client = client or httpx.Client(
            follow_redirects=False,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
        super().__init__(settings, transport=self.post)

    def close(self):
        self.client.close()

    def post(self, endpoint, payload, headers, timeout_seconds):
        try:
            with self.client.stream(
                "POST",
                endpoint,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(timeout_seconds, connect=2),
            ) as response:
                if response.status_code != 200:
                    raise LLMProviderError(
                        "Provider rejected request",
                        status_code=response.status_code,
                        retryable=response.status_code == 429,
                    )
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise LLMProviderError("Provider response exceeded size limit")
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("Response must be an object")
                return body
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMProviderError("Provider outcome unknown", retryable=False) from exc
