"""Validation for coordinated native runtime evidence, including unsent requests."""

import math
from dataclasses import asdict
from typing import Any, Dict

from forge.admission import AdmissionOptions
from forge.inference import parse_inference_info

ADMISSION_REJECTIONS = {"admission_queue_full", "admission_timeout", "token_budget_exceeded",
                        "retry_budget_exhausted", "circuit_open"}


def validate_coordinated_report(report: Dict[str, Any]) -> None:
    from forge.native_runtime import NativeRuntimeOptions, retry_delay

    runtime, results = report["runtime"], report["results"]
    for key, cls in (("options", NativeRuntimeOptions), ("coordination", AdmissionOptions)):
        raw = runtime.get(key)
        if not isinstance(raw, dict) or set(raw) != set(asdict(cls())):
            raise ValueError("invalid coordinated runtime settings")
        cls(**raw)
    options = NativeRuntimeOptions(**runtime["options"])

    def duration(value: Any) -> bool:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    def integer(value: Any) -> bool:
        return type(value) is int and value >= 0

    elapsed, rate = runtime.get("duration_seconds"), runtime.get("completed_tasks_per_second")
    if (not duration(elapsed) or elapsed <= 0 or not duration(rate)
            or not math.isclose(rate, len(results) / elapsed, rel_tol=1e-9)):
        raise ValueError("invalid coordinated runtime duration/rate")
    total = retries = rejections = retry_rejections = reserved = charged = 0
    admission_seconds = 0.0
    for index, result in enumerate(results):
        attempts, terminal = result.get("attempts"), result.get("termination")
        if (not isinstance(attempts, list) or len(attempts) > options.max_attempts
                or any(not duration(result.get(key)) for key in
                       ("duration_seconds", "queue_seconds", "admission_seconds"))
                or result["duration_seconds"] + result["queue_seconds"] > elapsed + 1e-9
                or result["admission_seconds"] > result["duration_seconds"] + result["queue_seconds"] + 1e-9
                or (result.get("error") is not None and result.get("answer") is not None)):
            raise ValueError("invalid coordinated result")
        rejected = terminal in ADMISSION_REJECTIONS
        if rejected:
            if result["error"] != "provider admission rejected" or result.get("answer") is not None:
                raise ValueError("invalid admission rejection")
            rejections += 1
        elif not attempts:
            raise ValueError("non-rejected task has no attempts")
        for number, attempt in enumerate(attempts, 1):
            fields = {"number", "error", "retryable", "timed_out", "retry_after_seconds", "retry_delay_seconds",
                      "duration_seconds", "admission_seconds", "admitted_at_seconds", "reserved_tokens",
                      "charged_tokens", "reported_tokens", "rate_limited"}
            if (not isinstance(attempt, dict) or set(attempt) not in (fields, fields | {"inference"})
                    or type(attempt["number"]) is not int or attempt["number"] != number
                    or any(type(attempt[key]) is not bool for key in ("retryable", "timed_out", "rate_limited"))
                    or any(not duration(attempt[key]) for key in ("duration_seconds", "admission_seconds",
                                                                  "admitted_at_seconds", "retry_after_seconds"))
                    or any(not integer(attempt[key]) for key in ("reserved_tokens", "charged_tokens"))
                    or attempt["reserved_tokens"] == 0
                    or (attempt["reported_tokens"] is not None and not integer(attempt["reported_tokens"]))
                    or attempt["charged_tokens"] != (attempt["reported_tokens"] if attempt["reported_tokens"] is not None
                                                       else attempt["reserved_tokens"])
                    or attempt["error"] not in (None, "provider request failed")
                    or (attempt["retryable"] and attempt["error"] is None)
                    or ((attempt["timed_out"] or attempt["rate_limited"]) and not attempt["retryable"])
                    or attempt["admitted_at_seconds"] + attempt["duration_seconds"] > elapsed + 1e-9):
                raise ValueError("invalid coordinated attempt")
            if "inference" in attempt:
                info = parse_inference_info(attempt["inference"], attempt["reported_tokens"])
                if info.stream is not None and info.stream.duration_seconds > attempt["duration_seconds"] + 1e-9:
                    raise ValueError("stream duration exceeds its attempt")
            scheduled = number < len(attempts) or (number == len(attempts) and rejected)
            if scheduled:
                if (not attempt["retryable"] or number >= options.max_attempts
                        or not duration(attempt["retry_delay_seconds"]) or attempt["retry_delay_seconds"] !=
                        retry_delay(options, index, number, attempt["retry_after_seconds"])):
                    raise ValueError("invalid coordinated retry schedule")
            elif attempt["retry_delay_seconds"] is not None:
                raise ValueError("unexpected terminal retry schedule")
            reserved += attempt["reserved_tokens"]
            charged += attempt["charged_tokens"]
        if attempts:
            last = attempts[-1]
            if not rejected:
                if last["error"] != result["error"]:
                    raise ValueError("attempt and task errors disagree")
                if not last["retryable"]:
                    expected = "answered" if last["error"] is None else "provider_error"
                elif len(attempts) == options.max_attempts:
                    expected = "attempts_exhausted"
                elif last["retry_after_seconds"] > options.max_retry_after_seconds:
                    expected = "retry_after_limit"
                else:
                    expected = "retry_queue_full"
                if terminal != expected:
                    raise ValueError("invalid coordinated termination")
            retry_rejections += rejected or terminal in ("retry_queue_full", "retry_after_limit")
            if (sum(a["duration_seconds"] for a in attempts) > result["duration_seconds"] + 1e-9
                    or sum(a["admission_seconds"] for a in attempts) > result["admission_seconds"] + 1e-9):
                raise ValueError("inconsistent coordinated timing")
        total += len(attempts)
        retries += max(0, len(attempts) - 1)
        admission_seconds += result["admission_seconds"]
    for key, expected in (("attempts", total), ("retries", retries), ("admission_rejections", rejections),
                           ("retry_rejections", retry_rejections), ("reserved_tokens", reserved),
                           ("charged_tokens", charged), ("peak_admission_queue", len(results))):
        if not integer(runtime.get(key)) or runtime[key] != expected:
            raise ValueError("inconsistent coordinated runtime counts")
    mean = runtime.get("mean_admission_queue")
    if not duration(mean) or not math.isclose(mean, admission_seconds / elapsed, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError("inconsistent admission queue mean")
    for key, high in (("peak_in_flight", min(options.concurrency, total)),
                       ("peak_pending_retries", options.max_pending_retries)):
        if not integer(runtime.get(key)) or runtime[key] > high:
            raise ValueError("invalid coordinated peak")
    scope = runtime.get("scope")
    if (not isinstance(scope, dict) or scope.get("circuit") not in ("closed", "open", "half_open")
            or any(not integer(scope.get(key)) for key in
                   ("requests", "retries", "charged_tokens", "circuit_opens", "peak_waiters"))
            or scope["requests"] < total or scope["retries"] < retries):
        raise ValueError("invalid provider scope counters")
