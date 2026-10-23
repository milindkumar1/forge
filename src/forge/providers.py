"""Provider interface, offline mock, and explicitly gated OpenRouter transport."""

import json
import os
import urllib.request
from typing import Protocol

from forge.config import Config


class ProviderError(Exception):
    """A sanitized provider failure that can safely appear in reports."""


class Provider(Protocol):
    def complete(self, prompt: str) -> str:
        ...


class MockProvider:
    def __init__(self, mode: str = "correct") -> None:
        if mode not in ("correct", "faulty"):
            raise ValueError("unsupported mock mode")
        self.mode = mode

    def complete(self, prompt: str) -> str:
        if self.mode == "faulty":
            return "incorrect"
        data = json.loads(prompt)
        operation, text = data["operation"], data["text"]
        if operation == "reverse":
            return text[::-1]
        if operation == "word_count":
            return str(len(text.split()))
        if operation == "sort_numbers":
            return ",".join(str(n) for n in sorted(int(n) for n in text.split(",")))
        raise ProviderError("unsupported mock operation")


class OpenRouterProvider:
    def __init__(self, model: str, *, allow_network: bool = False) -> None:
        if not allow_network:
            raise ValueError("OpenRouter requires explicit --allow-network (may incur API costs)")
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise ValueError("set OPENROUTER_API_KEY before using OpenRouter")
        self.model = model
        self._key = key

    def complete(self, prompt: str) -> str:
        request = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps({
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 256,
            }).encode("utf-8"),
            headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read(1_000_001))
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("non-text response")
            return content
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            # Do not include the response body, request headers, or exception text.
            raise ProviderError("OpenRouter request failed or returned an invalid response") from exc


def make_provider(config: Config, *, allow_network: bool = False) -> Provider:
    if config.provider == "mock":
        return MockProvider(config.mock_mode)
    return OpenRouterProvider(config.model, allow_network=allow_network)
