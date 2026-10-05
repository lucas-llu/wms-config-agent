from dataclasses import replace

import pytest

from core.settings import load_settings
from libs.llm import LLMProviderError, OpenAICompatibleLLM
from libs.llm.session_context import llm_conversation, provider_session_id


def test_context_is_stable_nested_and_restored():
    with llm_conversation("workspace:a:conversation"):
        first = provider_session_id()
        assert "workspace" not in first
        with llm_conversation("workspace:b:conversation"):
            assert provider_session_id() != first
        assert provider_session_id() == first
    with llm_conversation("workspace:a:conversation"):
        assert provider_session_id() == first
    assert provider_session_id() != provider_session_id()


@pytest.mark.parametrize("host", ["opencode.ai", "provider.example"])
def test_header_is_host_scoped_and_stable_on_retry(host):
    headers_seen = []

    def transport(endpoint, payload, headers, timeout):
        headers_seen.append(dict(headers))
        if len(headers_seen) == 1:
            raise LLMProviderError("retry", retryable=True)
        return {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}

    settings = replace(
        load_settings().llm, base_url=f"https://{host}/v1", api_key_env=None, max_retries=1
    )
    llm = OpenAICompatibleLLM(settings, transport=transport, sleeper=lambda _: None)
    with llm_conversation("conversation"):
        llm.chat([{"role": "user", "content": "synthetic"}])
    assert headers_seen[0] == headers_seen[1]
    assert ("x-opencode-session" in headers_seen[0]) == (host == "opencode.ai")
    assert headers_seen[0]["User-Agent"] == "wms-config-agent/0.1"
