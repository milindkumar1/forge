"""Bounded SSE chat decoding; output remains data and deltas are not tokens."""

import codecs
import json
from typing import AsyncIterable, AsyncIterator, Callable, List

from forge.inference import InferenceInfo, MAX_CONTENT_DELTAS, StreamTrace, token_usage
from forge.providers import Completion, ProviderError, RetryableProviderError

MAX_STREAM_BYTES = 1_000_000
MAX_EVENT_CHARS = 65536


async def sse_data(chunks: AsyncIterable[bytes]) -> AsyncIterator[str]:
    """Handle UTF-8/BOM, arbitrary byte splits, CR/LF/CRLF and multiline data."""
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    line: List[str] = []
    data: List[str] = []
    size = total = 0
    skip_lf = False
    async for chunk in chunks:
        total += len(chunk)
        if total > MAX_STREAM_BYTES:
            raise ProviderError("provider stream too large")
        for char in decoder.decode(chunk):
            if skip_lf:
                skip_lf = False
                if char == "\n":
                    continue
            if char not in ("\r", "\n"):
                line.append(char)
                if len(line) > MAX_EVENT_CHARS:
                    raise ProviderError("provider stream line too large")
                continue
            skip_lf = char == "\r"
            value = "".join(line)
            line.clear()
            if value == "":
                if data:
                    yield "\n".join(data)
                data.clear()
                size = 0
            else:
                field, _, value = value.partition(":")
                if field == "data":
                    value = value[1:] if value.startswith(" ") else value
                    size += len(value) + 1
                    if size > MAX_EVENT_CHARS:
                        raise ProviderError("provider stream event too large")
                    data.append(value)
    decoder.decode(b"", final=True)  # Reject incomplete UTF-8; EOF does not dispatch an unfinished event.


async def read_chat_stream(chunks: AsyncIterable[bytes], *, model: str,
                           now: Callable[[], float], started: float) -> Completion:
    text: List[str] = []
    arrivals: List[float] = []
    total = prompt = output = None
    response_model = finish = None
    async for raw in sse_data(chunks):
        if raw == "[DONE]":
            if finish is None:
                raise ProviderError("provider stream has no finish reason")
            trace = StreamTrace(tuple(arrivals), now() - started, finish)
            return Completion("".join(text), total, InferenceInfo(prompt, output, response_model, trace))
        payload = json.loads(raw)
        if not isinstance(payload, dict) or "error" in payload:
            raise ProviderError("provider stream returned an invalid event")
        if payload.get("model") is not None:
            if payload["model"] != model:
                raise ProviderError("provider returned an unexpected model")
            response_model = payload["model"]
        if payload.get("usage") is not None:
            total, prompt, output = token_usage(payload)
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise ProviderError("provider stream has invalid choices")
        if not choices:
            continue  # Final usage-only event.
        choice = choices[0]
        if not isinstance(choice, dict) or type(choice.get("index")) is not int or choice["index"] != 0:
            raise ProviderError("provider stream has an invalid choice")
        delta = choice.get("delta")
        if (not isinstance(delta, dict) or delta.get("tool_calls") is not None
                or delta.get("function_call") is not None):
            raise ProviderError("provider stream requires text deltas")
        content = delta.get("content")
        if content is not None and not isinstance(content, str):
            raise ProviderError("provider stream returned non-text content")
        if content:
            if finish is not None or len(arrivals) >= MAX_CONTENT_DELTAS:
                raise ProviderError("provider stream has excessive or late content")
            text.append(content)
            arrivals.append(now() - started)
        reason = choice.get("finish_reason")
        if reason is not None:
            if reason not in ("stop", "length") or finish is not None:
                raise ProviderError("provider stream has an invalid finish reason")
            finish = reason
    raise RetryableProviderError("provider stream ended before completion")
