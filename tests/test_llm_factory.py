"""Provider selection and HTTP payload compatibility."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from llm.factory import create_llm_client, resolve_llm_model, resolve_llm_provider
from llm.schemas import BUILDSPEC_RESPONSE_FORMAT
from ui.server import create_app


class _Response:
    status_code = 200

    def json(self) -> dict:
        return {"choices": [{"message": {"content": '{"ok":true}'}}]}


def test_ollama_cloud_uses_its_key_and_omits_unsupported_response_format(monkeypatch) -> None:
    monkeypatch.setenv("AWARE_LLM_PROVIDER", "ollama-cloud")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-secret")
    monkeypatch.setenv("OLLAMA_MODEL", "cloud-model")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls: list[dict] = []

    def post(url, *, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "payload": json, "timeout": timeout})
        return _Response()

    monkeypatch.setattr("llm.openai_compatible.requests.post", post)
    assert resolve_llm_provider() == "ollama-cloud"
    assert resolve_llm_model() == "cloud-model"
    client = create_llm_client()
    assert client.provider_name == "ollama-cloud"
    assert client.complete("System", "User", BUILDSPEC_RESPONSE_FORMAT) == '{"ok":true}'

    call = calls[0]
    assert call["url"] == "https://ollama.com/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer ollama-secret"
    assert call["payload"]["model"] == "cloud-model"
    assert call["timeout"] == 120.0
    assert "response_format" not in call["payload"]
    assert '"task_type"' in call["payload"]["messages"][0]["content"]


def test_openai_keeps_structured_response_format(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    calls: list[dict] = []

    def post(url, *, headers, json, timeout):
        calls.append(json)
        return _Response()

    monkeypatch.setattr("llm.openai_compatible.requests.post", post)
    client = create_llm_client(provider="openai-compatible", openai_model="gpt-test")
    client.complete("System", "User", BUILDSPEC_RESPONSE_FORMAT)

    assert calls[0]["response_format"] == BUILDSPEC_RESPONSE_FORMAT
    assert calls[0]["messages"][0]["content"] == "System"


def test_ollama_cloud_requires_key_and_model(monkeypatch) -> None:
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    with pytest.raises(ValueError, match="OLLAMA_API_KEY"):
        create_llm_client(provider="ollama-cloud")
    monkeypatch.setenv("OLLAMA_API_KEY", "key")
    with pytest.raises(ValueError, match="OLLAMA_MODEL"):
        create_llm_client(provider="ollama-cloud")


def test_ollama_alias_uses_cloud_configuration(monkeypatch) -> None:
    monkeypatch.setenv("AWARE_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_API_KEY", "key")
    monkeypatch.setenv("OLLAMA_MODEL", "cloud-model")

    assert resolve_llm_provider() == "ollama-cloud"
    assert resolve_llm_model(provider="ollama") == "cloud-model"
    assert create_llm_client(provider="ollama").provider_name == "ollama-cloud"


def test_ui_sidebar_hides_advanced_controls_and_ready_hint(monkeypatch) -> None:
    monkeypatch.setenv("AWARE_LLM_PROVIDER", "openai-compatible")
    html = TestClient(create_app()).get("/").text

    assert '<select id="timezoneOffset" class="hidden"' in html
    assert '<input id="maxAgents" type="hidden" value="">' in html
    assert '<label for="timezoneOffset">' not in html
    assert '<label for="maxAgents">' not in html
    assert 'incidents ready. Click Analyze batch to start.' not in html


def test_ui_config_reports_selected_cloud_provider_without_exposing_key(monkeypatch) -> None:
    monkeypatch.setenv("AWARE_LLM_PROVIDER", "ollama-cloud")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-secret")
    monkeypatch.setenv("OLLAMA_MODEL", "cloud-model")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")
    config = TestClient(create_app()).get("/api/config").json()

    assert config["parser_llm_provider"] == "ollama-cloud"
    assert config["openai_model"] == "cloud-model"
    assert config["llm_provider"] == "ollama-cloud"
    assert config["llm_model"] == "cloud-model"
    assert "ollama-secret" not in str(config)
