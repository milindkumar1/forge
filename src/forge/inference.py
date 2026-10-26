"""Validated provider usage and client-observed stream timing evidence."""

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

MAX_CONTENT_DELTAS = 10000


def _count(value: Any) -> bool:
    return value is None or (type(value) is int and value >= 0)


def _duration(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


@dataclass(frozen=True)
class StreamTrace:
    content_arrival_seconds: Tuple[float, ...]
    duration_seconds: float
    finish_reason: str

    def __post_init__(self) -> None:
        times = self.content_arrival_seconds
        if (not isinstance(times, tuple) or len(times) > MAX_CONTENT_DELTAS or not _duration(self.duration_seconds)
                or any(not _duration(value) or value > self.duration_seconds for value in times)
                or any(right < left for left, right in zip(times, times[1:]))
                or self.finish_reason not in ("stop", "length")):
            raise ValueError("invalid stream timing evidence")

    def to_dict(self) -> Dict[str, Any]:
        return {"timing_unit": "content_delta", "content_arrival_seconds": list(self.content_arrival_seconds),
                "duration_seconds": self.duration_seconds, "finish_reason": self.finish_reason}


@dataclass(frozen=True)
class InferenceInfo:
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    response_model: Optional[str] = None
    stream: Optional[StreamTrace] = None

    def __post_init__(self) -> None:
        if (not _count(self.prompt_tokens) or not _count(self.completion_tokens)
                or (self.response_model is not None and (not isinstance(self.response_model, str)
                    or not self.response_model or len(self.response_model) > 512))
                or (self.stream is not None and not isinstance(self.stream, StreamTrace))):
            raise ValueError("invalid inference metadata")

    def validate_total(self, total: Optional[int]) -> None:
        if (total is not None and self.prompt_tokens is not None and self.completion_tokens is not None
                and total != self.prompt_tokens + self.completion_tokens):
            raise ValueError("inconsistent provider token usage")

    def to_dict(self) -> Dict[str, Any]:
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "response_model": self.response_model, "stream": self.stream.to_dict() if self.stream else None}


def token_usage(payload: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    usage = payload.get("usage")
    if usage is None:
        return None, None, None
    if not isinstance(usage, dict):
        raise ValueError("invalid provider usage")
    total, prompt, output = (usage.get(key) for key in ("total_tokens", "prompt_tokens", "completion_tokens"))
    if not all(_count(value) for value in (total, prompt, output)):
        raise ValueError("invalid provider token counts")
    info = InferenceInfo(prompt, output)
    if total is None and prompt is not None and output is not None:
        total = prompt + output
    info.validate_total(total)
    return total, prompt, output


def parse_inference_info(data: Any, total: Optional[int]) -> InferenceInfo:
    if not isinstance(data, dict) or set(data) != {"prompt_tokens", "completion_tokens", "response_model", "stream"}:
        raise ValueError("invalid inference evidence")
    stream = data["stream"]
    if stream is not None:
        fields = {"timing_unit", "content_arrival_seconds", "duration_seconds", "finish_reason"}
        if (not isinstance(stream, dict) or set(stream) != fields or stream["timing_unit"] != "content_delta"
                or not isinstance(stream["content_arrival_seconds"], list)):
            raise ValueError("invalid stream evidence")
        stream = StreamTrace(tuple(stream["content_arrival_seconds"]), stream["duration_seconds"], stream["finish_reason"])
    info = InferenceInfo(data["prompt_tokens"], data["completion_tokens"], data["response_model"], stream)
    info.validate_total(total)
    return info
