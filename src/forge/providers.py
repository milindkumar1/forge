"""Provider interface, offline mock, and explicitly gated OpenRouter transport."""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional
from typing import Protocol

from forge.config import Config, ProviderConfig


class ProviderError(Exception):
    """A sanitized provider failure that can safely appear in reports."""


class RetryableProviderError(ProviderError):
    """A transient transport failure; retry only with an explicit attempt budget."""

    def __init__(self, message: str, *, retry_after_seconds: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderTimeoutError(RetryableProviderError):
    """The complete request exceeded its deadline."""


class ProviderRateLimitError(RetryableProviderError):
    """HTTP 429 feedback applies to the whole provider admission scope."""


@dataclass(frozen=True)
class Completion:
    text: str
    total_tokens: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or (self.total_tokens is not None and
                (type(self.total_tokens) is not int or self.total_tokens < 0)):
            raise ValueError("invalid completion or token usage")


class AsyncProvider(Protocol):
    async def acomplete(self, prompt: str) -> str:
        ...


class AsyncMockProvider:
    def __init__(self, mode: str = "correct") -> None:
        self._mock = MockProvider(mode)

    async def acomplete(self, prompt: str) -> str:
        return self._mock.complete(prompt)


class Provider(Protocol):
    # Concurrent runners share this object: implementations must be thread-safe.
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
        if data["operation"] == "tool_episode":
            for entry in data["trace"]:
                if entry["event"] == "tool_result":
                    inventory = json.loads(entry["value"])
                    return json.dumps({"answer": str(inventory["washers"])})
            return json.dumps({"tool": "read_fixture", "path": "inventory.json"})
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
        except urllib.error.HTTPError as exc:
            error_type = RetryableProviderError if exc.code in (429, 500, 502, 503, 504) else ProviderError
            if exc.fp is not None:
                exc.close()
            raise error_type("OpenRouter HTTP request failed") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise RetryableProviderError("OpenRouter transport request failed") from exc
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            # Do not include the response body, request headers, or exception text.
            raise ProviderError("OpenRouter request failed or returned an invalid response") from exc


def make_provider(config: ProviderConfig, *, allow_network: bool = False) -> Provider:
    if config.provider == "local":
        raise ValueError("local provider requires the native async runtime")
    if config.provider == "mock":
        return MockProvider(config.mock_mode)
    return OpenRouterProvider(config.model, allow_network=allow_network)
