"""LLM client factory."""

from __future__ import annotations

import os

from llm.base import LLMClient
from llm.openai_compatible import OpenAICompatibleLLMClient


def _normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    return "ollama-cloud" if normalized == "ollama" else normalized


def resolve_llm_provider() -> str:
    """Return the configured provider, preserving the OpenAI default."""
    return _normalize_provider(os.getenv("AWARE_LLM_PROVIDER") or "openai-compatible")


def resolve_llm_model(override: str | None = None, provider: str | None = None) -> str | None:
    """Resolve a CLI/UI model override against the selected provider."""
    if override and override.strip():
        return override.strip()
    if _normalize_provider(provider or resolve_llm_provider()) == "ollama-cloud":
        return os.getenv("OLLAMA_MODEL", "").strip() or None
    return os.getenv("OPENAI_MODEL", "").strip() or "gpt-5-mini"


def create_llm_client(
    provider: str | None = None,
    *,
    openai_api_key: str | None = None,
    openai_base_url: str | None = None,
    openai_model: str | None = None,
) -> LLMClient:
    """Create an OpenAI-compatible client for OpenAI or Ollama Cloud."""
    normalized = _normalize_provider(provider or resolve_llm_provider())
    if normalized == "ollama-cloud":
        api_key = os.getenv("OLLAMA_API_KEY")
        if not api_key:
            raise ValueError("OLLAMA_API_KEY is required for ollama-cloud provider.")
        model = openai_model or os.getenv("OLLAMA_MODEL")
        if not model:
            raise ValueError("OLLAMA_MODEL or --llm-model is required for ollama-cloud provider.")
        return OpenAICompatibleLLMClient(
            api_key=api_key,
            base_url=os.getenv("OLLAMA_BASE_URL") or "https://ollama.com/v1",
            model=model,
            timeout_seconds=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120")),
            supports_response_format=False,
            provider_name="ollama-cloud",
        )
    if normalized not in {"openai", "openai-compatible"}:
        raise ValueError(f"Unsupported LLM provider: {normalized}.")
    return OpenAICompatibleLLMClient(
        api_key=openai_api_key,
        base_url=openai_base_url,
        model=openai_model,
    )
