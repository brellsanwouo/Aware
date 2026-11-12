"""OpenAI-compatible LLM client."""

from __future__ import annotations

import json
import os
from typing import Any

import requests

from llm.base import LLMClient, LLMError
from llm.schemas import BUILDSPEC_RESPONSE_FORMAT


class OpenAICompatibleLLMClient(LLMClient):
    """Minimal chat-completions wrapper for OpenAI-compatible APIs."""

    provider_name = "openai-compatible"
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 30.0,
        supports_response_format: bool = True,
        provider_name: str = "openai-compatible",
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-5-mini")
        self.timeout_seconds = float(timeout_seconds)
        self.supports_response_format = supports_response_format
        self.provider_name = provider_name
        if not self.api_key:
            raise LLMError(f"API key is required for {self.provider_name} provider.")

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        url = f"{self.base_url}/chat/completions"
        chosen_format = response_format if response_format is not None else BUILDSPEC_RESPONSE_FORMAT
        if chosen_format is not None and not self.supports_response_format:
            schema = chosen_format.get("json_schema", {}).get("schema")
            if schema is not None:
                system_prompt += (
                    "\nReturn only a JSON object matching this schema: "
                    f"{json.dumps(schema, ensure_ascii=False)}"
                )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if chosen_format is not None and self.supports_response_format:
            payload["response_format"] = chosen_format
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        if response.status_code >= 400:
            raise LLMError(
                f"LLM request failed ({response.status_code}): {response.text[:300]}"
            )
        try:
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise LLMError("Unexpected OpenAI-compatible response format.") from exc
