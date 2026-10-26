"""Native async admission with independent active-request and retry bounds."""

import asyncio
import heapq
import math
import random
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Dict, List, Optional, Tuple, cast

from forge.config import Config, ProviderConfig
from forge.admission import Clock, Permit, ProviderCoordinator, Ticket
from forge.evaluation import build_report
from forge.providers import AsyncProvider, Completion, ProviderError, ProviderRateLimitError, ProviderTimeoutError, RetryableProviderError
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
    admission_seconds: float = 0.0


async def evaluate_native(config: ProviderConfig, provider: AsyncProvider, suite: TaskSuite = DEFAULT_SUITE,
                          options: NativeRuntimeOptions = NativeRuntimeOptions(), *,
                          coordinator: Optional[ProviderCoordinator] = None,
                          clock: Optional[Clock] = None) -> Dict[str, Any]:
    validation = validate_suite(suite)
    validation.require_valid()
    started_at = datetime.now(timezone.utc).isoformat()
    if coordinator is not None:
        if clock is not None and clock is not coordinator.clock:
            raise ValueError("scheduler and coordinator must share one clock")
        clock = coordinator.clock
    now = clock.now if clock is not None else perf_counter
    sleep = clock.sleep if clock is not None else asyncio.sleep
    begin = now()
    states = [_State() for _ in suite.tasks]
    results: List[Optional[Dict[str, Any]]] = [None] * len(suite.tasks)
    active: Dict[asyncio.Task, int] = {}
    pending: List[Tuple[float, int, int]] = []
    fresh = 0
    sequence = 0
    prefer_retry = False
    peak_active = peak_pending = 0
    permits: Dict[int, Permit] = {}
    candidate: Optional[Tuple[int, bool, float]] = None
    ticket: Optional[Ticket] = None
    peak_admission_queue = len(suite.tasks)

    async def request(index: int, admission_seconds: float) -> Tuple[Optional[str], Dict[str, Any]]:
        state = states[index]
        call_begin = now()
        answer = None
        error = None
        retryable = timed_out = False
        floor = 0.0
        actual_tokens = None
        inference = None
        rate_limited = False
        try:
            usage_method = getattr(provider, "acomplete_with_usage", None) if coordinator is not None else None
            if callable(usage_method):
                completion = await asyncio.wait_for(usage_method(suite.tasks[index].prompt),
                                                    options.request_timeout_seconds)
                if not isinstance(completion, Completion):
                    raise ProviderError("invalid completion metadata")
                answer, actual_tokens, inference = completion.text, completion.total_tokens, completion.inference
            else:
                answer = await asyncio.wait_for(provider.acomplete(suite.tasks[index].prompt),
                                                options.request_timeout_seconds)
            if not isinstance(answer, str):
                raise ProviderError("non-text answer")
        except (asyncio.TimeoutError, ProviderTimeoutError):
            answer, error, retryable, timed_out = None, "provider request failed", True, True
        except ProviderError as exc:
            answer, error = None, "provider request failed"
            retryable = isinstance(exc, RetryableProviderError)
            rate_limited = isinstance(exc, ProviderRateLimitError)
            if retryable:
                floor = exc.retry_after_seconds
                if type(floor) not in (int, float) or not math.isfinite(floor) or floor < 0:
                    raise ValueError("provider supplied invalid retry delay") from None
        finally:
            # Exceptions outside the expected provider boundary propagate, but
            # admitted capacity is still settled conservatively during cleanup.
            if coordinator is not None:
                permit = permits.pop(index)
                interrupted = sys.exc_info()[0] is not None
                safe_floor = floor if type(floor) in (int, float) and math.isfinite(floor) and floor >= 0 else 0
                cooldown = max(safe_floor, retry_delay(options, index, len(state.attempts) + 1, 0)
                               if rate_limited else 0.0)
                reserved = permit.tokens
                coordinator.complete(permit, actual_tokens=actual_tokens, failed=error is not None,
                                     cooldown_seconds=cooldown, cancelled=interrupted)
        attempt = {"number": len(state.attempts) + 1, "error": error,
                        "retryable": retryable, "timed_out": timed_out,
                        "retry_after_seconds": floor, "retry_delay_seconds": None,
                        "duration_seconds": now() - call_begin}
        if coordinator is not None:
            attempt.update(admission_seconds=admission_seconds, admitted_at_seconds=call_begin - begin,
                           reserved_tokens=reserved, charged_tokens=permit.tokens,
                           reported_tokens=actual_tokens, rate_limited=rate_limited)
            if inference is not None:
                attempt["inference"] = inference.to_dict()
        return answer, attempt

    def finish(index: int, answer: Optional[str], error: Optional[str], termination: str) -> None:
        state, task = states[index], suite.tasks[index]
        end = now()
        results[index] = {
            "task_id": task.task_id, "prompt": task.prompt, "expected": task.expected,
            "answer": answer, "error": error, "passed": error is None and task.verify(cast(str, answer)),
            "termination": termination, "attempts": state.attempts,
            "queue_seconds": (state.started if state.started is not None else end) - begin,
            "duration_seconds": end - state.started if state.started is not None else 0.0,
        }
        results[index]["admission_seconds"] = state.admission_seconds

    try:
        while fresh < len(suite.tasks) or active or pending or candidate is not None:
            admission_delay = None
            while len(active) < options.concurrency:
                if candidate is None:
                    due = bool(pending and pending[0][0] <= now())
                    if due and (prefer_retry or fresh == len(suite.tasks)):
                        ready_at, _, index = heapq.heappop(pending)
                        candidate = (index, True, ready_at)
                        prefer_retry = False
                    elif fresh < len(suite.tasks):
                        index = fresh
                        fresh += 1
                        candidate = (index, False, begin)
                        prefer_retry = True
                    else:
                        break
                index, is_retry, ready_at = candidate
                if coordinator is not None:
                    if ticket is None:
                        ticket = coordinator.enqueue(coordinator.options.reservation(suite.tasks[index].prompt),
                                                     retry=is_retry, ready_at=ready_at)
                        if ticket is None:
                            states[index].admission_seconds += now() - ready_at
                            finish(index, None, "provider admission rejected", "admission_queue_full")
                            candidate = None
                            continue
                    decision = coordinator.decide(ticket)
                    if decision.rejection is not None:
                        states[index].admission_seconds += now() - ready_at
                        finish(index, None, "provider admission rejected", decision.rejection)
                        ticket = candidate = None
                        continue
                    if decision.permit is None:
                        admission_delay = decision.delay
                        break
                    permits[index] = decision.permit
                    ticket = None
                admitted_at = now()
                admission_seconds = admitted_at - ready_at
                states[index].admission_seconds += admission_seconds
                if states[index].started is None:
                    states[index].started = admitted_at
                active[asyncio.create_task(request(index, admission_seconds))] = index
                candidate = None
                peak_active = max(peak_active, len(active))
            # A timer is relevant only when there is an idle request slot.
            delay = (max(0.0, pending[0][0] - now())
                     if pending and len(active) < options.concurrency else None)
            if candidate is not None:
                delay = admission_delay
            if not active:
                if delay is not None:
                    await (coordinator.wait(delay) if coordinator is not None else sleep(delay))
                continue
            if coordinator is None and (clock is None or delay is None):
                done, _ = await asyncio.wait(active, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
            else:
                waiter = asyncio.create_task(coordinator.wait(delay) if coordinator is not None else sleep(delay))
                try:
                    done, _ = await asyncio.wait([*active, waiter], return_when=asyncio.FIRST_COMPLETED)
                    done.discard(waiter)
                finally:
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
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
                elif len(pending) + int(candidate is not None and candidate[1]) >= options.max_pending_retries:
                    finish(index, None, error, "retry_queue_full")
                else:
                    wait = retry_delay(options, index, attempt["number"], attempt["retry_after_seconds"])
                    attempt["retry_delay_seconds"] = wait
                    heapq.heappush(pending, (now() + wait, sequence, index))
                    sequence += 1
                    peak_pending = max(peak_pending, len(pending) + int(candidate is not None and candidate[1]))
    finally:
        if coordinator is not None and ticket is not None:
            coordinator.cancel(ticket)
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        # A task cancelled before its coroutine starts never enters request().
        if coordinator is not None:
            for permit in permits.values():
                coordinator.complete(permit, cancelled=True)
    elapsed = max(now() - begin, 1e-12)
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
        "peak_admission_queue": peak_admission_queue,
        "mean_admission_queue": sum(r["admission_seconds"] for r in completed_results) / elapsed,
    }
    if coordinator is not None:
        from forge.admission_reports import ADMISSION_REJECTIONS

        report["runtime"].update(
            kind="native-async-v2", coordination=asdict(coordinator.options),
            retries=sum(max(0, len(r["attempts"]) - 1) for r in completed_results),
            admission_rejections=sum(r["termination"] in ADMISSION_REJECTIONS for r in completed_results),
            retry_rejections=sum(r["termination"] in ("retry_queue_full", "retry_after_limit") or
                                 (bool(r["attempts"]) and r["termination"] in ADMISSION_REJECTIONS)
                                 for r in completed_results),
            reserved_tokens=sum(a["reserved_tokens"] for r in completed_results for a in r["attempts"]),
            charged_tokens=sum(a["charged_tokens"] for r in completed_results for a in r["attempts"]),
            peak_admission_queue=peak_admission_queue,
            mean_admission_queue=sum(r["admission_seconds"] for r in completed_results) / elapsed,
            scope={"requests": coordinator.requests, "retries": coordinator.retries,
                   "charged_tokens": coordinator.charged_tokens, "circuit": coordinator.circuit,
                   "circuit_opens": coordinator.circuit_opens, "peak_waiters": coordinator.peak_waiters})
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
