"""Native async admission with independent active-request and retry bounds."""

import asyncio
import heapq
import math
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Dict, List, Optional, Tuple, cast

from forge.config import Config
from forge.evaluation import build_report
from forge.providers import AsyncProvider, ProviderError, ProviderTimeoutError, RetryableProviderError
from forge.runtime import RuntimeOptions
from forge.suites import DEFAULT_SUITE, TaskSuite, validate_suite


@dataclass(frozen=True)
class NativeRuntimeOptions(RuntimeOptions):
    max_pending_retries: int = 64
    request_timeout_seconds: float = 30.0
    retry_seed: int = 0
    max_retry_after_seconds: float = 300.0

    def __post_init__(self) -> None:
        super().__post_init__()
        if type(self.max_pending_retries) is not int or not 0 <= self.max_pending_retries <= 1000:
            raise ValueError("max_pending_retries must be an integer in [0, 1000]")
        if type(self.retry_seed) is not int or not 0 <= self.retry_seed <= 2**32 - 1:
            raise ValueError("retry_seed must be an integer in [0, 4294967295]")
        for name in ("request_timeout_seconds", "max_retry_after_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 3600:
                raise ValueError(name + " must be finite in (0, 3600]")


def retry_delay(options: NativeRuntimeOptions, index: int, number: int, server_floor: float) -> float:
    """Full jitter with per-task/attempt seeds, independent of completion order."""
    cap = min(60.0, options.retry_delay_seconds * 2 ** (number - 1))
    rng = random.Random("{}:{}:{}".format(options.retry_seed, index, number))
    return max(server_floor, rng.uniform(0, cap))


@dataclass
class _State:
    started: Optional[float] = None
    attempts: List[Dict[str, Any]] = field(default_factory=list)


async def evaluate_native(config: Config, provider: AsyncProvider, suite: TaskSuite = DEFAULT_SUITE,
                          options: NativeRuntimeOptions = NativeRuntimeOptions()) -> Dict[str, Any]:
    validation = validate_suite(suite)
    validation.require_valid()
    started_at = datetime.now(timezone.utc).isoformat()
    begin = perf_counter()
    states = [_State() for _ in suite.tasks]
    results: List[Optional[Dict[str, Any]]] = [None] * len(suite.tasks)
    active: Dict[asyncio.Task, int] = {}
    pending: List[Tuple[float, int, int]] = []
    fresh = 0
    sequence = 0
    prefer_retry = False
    peak_active = peak_pending = 0

    async def request(index: int) -> Tuple[Optional[str], Dict[str, Any]]:
        state = states[index]
        call_begin = perf_counter()
        answer = None
        error = None
        retryable = timed_out = False
        floor = 0.0
        try:
            answer = await asyncio.wait_for(provider.acomplete(suite.tasks[index].prompt),
                                            options.request_timeout_seconds)
            if not isinstance(answer, str):
                raise ProviderError("non-text answer")
        except (asyncio.TimeoutError, ProviderTimeoutError):
            answer, error, retryable, timed_out = None, "provider request failed", True, True
        except ProviderError as exc:
            answer, error = None, "provider request failed"
            retryable = isinstance(exc, RetryableProviderError)
            if retryable:
                floor = exc.retry_after_seconds
                if type(floor) not in (int, float) or not math.isfinite(floor) or floor < 0:
                    raise ValueError("provider supplied invalid retry delay") from None
        return answer, {"number": len(state.attempts) + 1, "error": error,
                        "retryable": retryable, "timed_out": timed_out,
                        "retry_after_seconds": floor, "retry_delay_seconds": None,
                        "duration_seconds": perf_counter() - call_begin}

    def finish(index: int, answer: Optional[str], error: Optional[str], termination: str) -> None:
        state, task = states[index], suite.tasks[index]
        assert state.started is not None
        results[index] = {
            "task_id": task.task_id, "prompt": task.prompt, "expected": task.expected,
            "answer": answer, "error": error, "passed": error is None and task.verify(cast(str, answer)),
            "termination": termination, "attempts": state.attempts,
            "queue_seconds": state.started - begin, "duration_seconds": perf_counter() - state.started,
        }

    try:
        while fresh < len(suite.tasks) or active or pending:
            while len(active) < options.concurrency:
                due = bool(pending and pending[0][0] <= perf_counter())
                if due and (prefer_retry or fresh == len(suite.tasks)):
                    _, _, index = heapq.heappop(pending)
                    prefer_retry = False
                elif fresh < len(suite.tasks):
                    index = fresh
                    fresh += 1
                    states[index].started = perf_counter()
                    prefer_retry = True
                else:
                    break
                active[asyncio.create_task(request(index))] = index
                peak_active = max(peak_active, len(active))
            # A timer is relevant only when there is an idle request slot.
            delay = (max(0.0, pending[0][0] - perf_counter())
                     if pending and len(active) < options.concurrency else None)
            if not active:
                if delay is not None:
                    await asyncio.sleep(delay)
                continue
            done, _ = await asyncio.wait(active, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
            # Stable processing order also makes simultaneous overload decisions reproducible.
            for completed in sorted(done, key=lambda task: active[task]):
                index = active.pop(completed)
                answer, attempt = completed.result()
                states[index].attempts.append(attempt)
                error = attempt["error"]
                if not attempt["retryable"]:
                    finish(index, answer, error, "answered" if error is None else "provider_error")
                elif attempt["number"] == options.max_attempts:
                    finish(index, None, error, "attempts_exhausted")
                elif attempt["retry_after_seconds"] > options.max_retry_after_seconds:
                    finish(index, None, error, "retry_after_limit")
                elif len(pending) >= options.max_pending_retries:
                    finish(index, None, error, "retry_queue_full")
                else:
                    wait = retry_delay(options, index, attempt["number"], attempt["retry_after_seconds"])
                    attempt["retry_delay_seconds"] = wait
                    heapq.heappush(pending, (perf_counter() + wait, sequence, index))
                    sequence += 1
                    peak_pending = max(peak_pending, len(pending))
    finally:
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
    elapsed = perf_counter() - begin
    completed_results = cast(List[Dict[str, Any]], results)
    attempts = sum(len(result["attempts"]) for result in completed_results)
    report = build_report(config, suite, validation, started_at, completed_results)
    report["runtime"] = {
        "kind": "native-async-v1", "options": asdict(options), "duration_seconds": elapsed,
        "peak_in_flight": peak_active, "peak_pending_retries": peak_pending,
        "attempts": attempts, "retries": attempts - len(results),
        "retry_rejections": sum(r["termination"] in ("retry_queue_full", "retry_after_limit")
                                for r in completed_results),
        "completed_tasks_per_second": len(results) / elapsed,
    }
    return report


def validate_native_report(report: Dict[str, Any]) -> None:
    runtime = report["runtime"]
    raw = runtime.get("options")
    if not isinstance(raw, dict) or set(raw) != set(asdict(NativeRuntimeOptions())):
        raise ValueError("invalid native runtime options")
    options = NativeRuntimeOptions(**raw)

    def duration(value: Any) -> bool:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    elapsed, rate = runtime.get("duration_seconds"), runtime.get("completed_tasks_per_second")
    if (not duration(elapsed) or elapsed <= 0 or not duration(rate)
            or not math.isclose(rate, len(report["results"]) / elapsed, rel_tol=1e-9)):
        raise ValueError("invalid native runtime duration or rate")
    for key, low, high in (("peak_in_flight", 1, min(options.concurrency, len(report["results"]))),
                           ("peak_pending_retries", 0, options.max_pending_retries)):
        if type(runtime.get(key)) is not int or not low <= runtime[key] <= high:
            raise ValueError("invalid native runtime peak")
    total = rejections = 0
    for index, result in enumerate(report["results"]):
        attempts = result.get("attempts")
        if (not duration(result.get("queue_seconds")) or not duration(result.get("duration_seconds"))
                or result["queue_seconds"] + result["duration_seconds"] > elapsed
                or not isinstance(attempts, list) or not 1 <= len(attempts) <= options.max_attempts):
            raise ValueError("invalid native runtime result")
        for number, attempt in enumerate(attempts, 1):
            fields = {"number", "error", "retryable", "timed_out", "retry_after_seconds",
                      "retry_delay_seconds", "duration_seconds"}
            if (not isinstance(attempt, dict) or set(attempt) != fields
                    or type(attempt["number"]) is not int or attempt["number"] != number
                    or attempt["error"] not in (None, "provider request failed")
                    or type(attempt["retryable"]) is not bool or type(attempt["timed_out"]) is not bool
                    or not duration(attempt["duration_seconds"]) or not duration(attempt["retry_after_seconds"])
                    or (attempt["retryable"] and attempt["error"] is None)
                    or (not attempt["retryable"] and attempt["retry_after_seconds"] != 0)
                    or (attempt["timed_out"] and not attempt["retryable"])):
                raise ValueError("invalid native attempt")
            if number < len(attempts):
                expected_delay = retry_delay(options, index, number, attempt["retry_after_seconds"])
                if (not attempt["retryable"] or not duration(attempt["retry_delay_seconds"])
                        or attempt["retry_delay_seconds"] != expected_delay
                        or attempt["retry_after_seconds"] > options.max_retry_after_seconds):
                    raise ValueError("invalid native retry")
            elif attempt["retry_delay_seconds"] is not None:
                raise ValueError("terminal attempt schedules another retry")
        last = attempts[-1]
        terminal = result.get("termination")
        if (last["error"] != result["error"] or (result["error"] is not None and result.get("answer") is not None)
                or sum(a["duration_seconds"] for a in attempts) > result["duration_seconds"]
                or sum(a["duration_seconds"] + (a["retry_delay_seconds"] or 0) for a in attempts)
                > result["duration_seconds"] + 1e-9):
            raise ValueError("native attempts disagree with result")
        if not last["retryable"]:
            expected = "answered" if last["error"] is None else "provider_error"
        elif len(attempts) == options.max_attempts:
            expected = "attempts_exhausted"
        elif last["retry_after_seconds"] > options.max_retry_after_seconds:
            expected = "retry_after_limit"
        else:
            expected = "retry_queue_full"
        if terminal != expected:
            raise ValueError("invalid native termination")
        if terminal == "retry_queue_full" and runtime["peak_pending_retries"] != options.max_pending_retries:
            raise ValueError("retry overflow disagrees with queue peak")
        rejections += terminal in ("retry_queue_full", "retry_after_limit")
        total += len(attempts)
    for name, expected in (("attempts", total), ("retries", total - len(report["results"])),
                           ("retry_rejections", rejections)):
        if type(runtime.get(name)) is not int or runtime[name] != expected:
            raise ValueError("invalid native runtime counts")
