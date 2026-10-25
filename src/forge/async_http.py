"""Optional native async OpenRouter transport; synchronous providers stay intact."""

import asyncio
import json
import math
import os
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Optional

try:
    import aiohttp
except ImportError as exc:
    raise ImportError("native HTTP requires: pip install 'forge-evals[http]'") from exc

from forge.providers import ProviderError, ProviderTimeoutError, RetryableProviderError

MAX_RESPONSE_BYTES = 1_000_000


def parse_retry_after(value: Optional[str], *, now: Optional[float] = None) -> float:
    """RFC 9110 delay-seconds or HTTP-date; invalid/past values mean no floor.

    Huge numeric delays remain above scheduler policy limits, never truncated
    into an early retry. Bound parsing work for untrusted headers.
    """
    if value is None:
        return 0.0
    value = value.strip()
    if value.isascii() and value.isdigit():
        return float(value) if len(value) <= 12 else 1e12
    if len(value) > 256:
        return 0.0
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0.0, date.timestamp() - (time.time() if now is None else now))
    except (ValueError, TypeError, OverflowError):
        return 0.0


class AsyncOpenRouterProvider:
    def __init__(self, model: str, *, allow_network: bool = False,
                 request_timeout_seconds: float = 30.0, max_connections: int = 4) -> None:
        if not allow_network:
            raise ValueError("OpenRouter requires explicit --allow-network (may incur API costs)")
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise ValueError("set OPENROUTER_API_KEY before using OpenRouter")
        if (type(request_timeout_seconds) not in (int, float)
                or not math.isfinite(request_timeout_seconds) or not 0 < request_timeout_seconds <= 3600):
            raise ValueError("request timeout must be finite in (0, 3600]")
        if type(max_connections) is not int or not 1 <= max_connections <= 64:
            raise ValueError("max_connections must be an integer in [1, 64]")
        self.model = model
        self._key = key
        self.request_timeout_seconds = request_timeout_seconds
        self.max_connections = max_connections
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "AsyncOpenRouterProvider":
        if self._session is not None:
            raise ValueError("provider session already open")
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=self.max_connections),
            timeout=aiohttp.ClientTimeout(total=None), trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def acomplete(self, prompt: str) -> str:
        if self._session is None:
            raise ValueError("use async with to open the provider session")
        try:
            return await asyncio.wait_for(self._request(prompt), self.request_timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise ProviderTimeoutError("provider request deadline exceeded") from exc
        except aiohttp.ClientError as exc:
            raise RetryableProviderError("provider transport failed") from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("provider returned an invalid response") from exc

    async def _request(self, prompt: str) -> str:
        assert self._session is not None
        async with self._session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json={"model": self.model, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0, "max_tokens": 256},
            headers={"Authorization": "Bearer " + self._key}, allow_redirects=False,
        ) as response:
            if response.status in (429, 500, 502, 503, 504):
                raise RetryableProviderError("provider HTTP request failed",
                    retry_after_seconds=parse_retry_after(response.headers.get("Retry-After")))
            if response.status != 200:
                raise ProviderError("provider HTTP request failed")
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ProviderError("provider response too large")
            payload = json.loads(body)
            answer = payload["choices"][0]["message"]["content"]
            if not isinstance(answer, str):
                raise ProviderError("provider returned a non-text answer")
            return answer
