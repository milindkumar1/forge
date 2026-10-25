"""Bounded asyncio scheduling over the existing blocking provider boundary."""

import asyncio
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Dict, List, Optional, cast

from forge.config import Config
from forge.evaluation import build_report
from forge.providers import Provider, ProviderError, RetryableProviderError
from forge.suites import DEFAULT_SUITE, TaskSuite, validate_suite


@dataclass(frozen=True)
class RuntimeOptions:
    concurrency: int = 1
    max_attempts: int = 1
    retry_delay_seconds: float = 0.25

    def __post_init__(self) -> None:
        if type(self.concurrency) is not int or not 1 <= self.concurrency <= 64:
            raise ValueError("concurrency must be an integer between 1 and 64")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be an integer between 1 and 5")
        if (type(self.retry_delay_seconds) not in (int, float)
                or not math.isfinite(self.retry_delay_seconds) or not 0 <= self.retry_delay_seconds <= 60):
            raise ValueError("retry_delay_seconds must be finite and between 0 and 60")


async def evaluate_async(config: Config, provider: Provider, suite: TaskSuite = DEFAULT_SUITE,
                         options: RuntimeOptions = RuntimeOptions()) -> Dict[str, Any]:
    """Keep O(concurrency) workers/futures and return results in suite order.

    Providers must support concurrent calls and bound their own blocking I/O.
    On cancellation or a programming error, stop admission and join in-flight
    calls before returning/raising. Python cannot forcibly stop running threads.
    """
    validation = validate_suite(suite)
    validation.require_valid()
    started = datetime.now(timezone.utc).isoformat()
    begin = perf_counter()
    results: List[Optional[Dict[str, Any]]] = [None] * len(suite.tasks)
    pending = iter(enumerate(suite.tasks))
    in_flight = 0
    peak = 0
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=options.concurrency, thread_name_prefix="forge-inference")

    async def worker() -> None:
        nonlocal in_flight, peak
        # Iteration and admission occur on the event-loop thread without awaits.
        for index, task in pending:
            task_begin = perf_counter()
            queue_seconds = task_begin - begin
            attempts: List[Dict[str, Any]] = []
            answer = None
            error = None
            for number in range(1, options.max_attempts + 1):
                attempt_begin = perf_counter()
                retryable = False
                in_flight += 1
                peak = max(peak, in_flight)
                try:
                    answer = await loop.run_in_executor(pool, provider.complete, task.prompt)
                    if not isinstance(answer, str):
                        raise ProviderError("non-text response")
                    error = None
                except ProviderError as exc:
                    answer, error = None, "provider request failed"
                    retryable = isinstance(exc, RetryableProviderError)
                finally:
                    in_flight -= 1
                attempts.append({"number": number, "error": error, "retryable": retryable,
                                 "duration_seconds": perf_counter() - attempt_begin})
                if not retryable or number == options.max_attempts:
                    break
                # Backoff holds the rollout slot, keeping retries within the same bound.
                await asyncio.sleep(min(60.0, options.retry_delay_seconds * 2 ** (number - 1)))
            results[index] = {
                "task_id": task.task_id, "prompt": task.prompt, "expected": task.expected,
                "answer": answer, "passed": error is None and answer is not None and task.verify(answer),
                "error": error, "duration_seconds": perf_counter() - task_begin,
                "queue_seconds": queue_seconds, "attempts": attempts,
            }

    workers = [asyncio.create_task(worker()) for _ in range(min(options.concurrency, len(suite.tasks)))]
    try:
        await asyncio.gather(*workers)
    finally:
        for task_worker in workers:
            task_worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        # Joining is intentional: never leave requests running after this run exits.
        # This can block the event loop until provider I/O finishes on cancellation.
        pool.shutdown(wait=True, cancel_futures=True)
    elapsed = perf_counter() - begin
    completed = cast(List[Dict[str, Any]], results)
    report = build_report(config, suite, validation, started, completed)
    report["runtime"] = {
        "kind": "async-thread-pool-v1", "options": asdict(options),
        "peak_in_flight": peak, "duration_seconds": elapsed,
        "attempts": sum(len(result["attempts"]) for result in completed),
        "completed_tasks_per_second": len(completed) / elapsed,
    }
    return report


def validate_runtime_report(report: Dict[str, Any]) -> None:
    """Check additive runtime evidence without changing legacy report support."""
    runtime = report["runtime"]
    if isinstance(runtime, dict) and runtime.get("kind") == "native-async-v1":
        if not isinstance(report.get("suite"), dict) or "tasks" not in report["suite"]:
            raise ValueError("runtime evidence requires a task snapshot")
        from forge.native_runtime import validate_native_report

        validate_native_report(report)
        return
    if not isinstance(runtime, dict) or runtime.get("kind") != "async-thread-pool-v1":
        raise ValueError("unsupported runtime evidence")
    if not isinstance(report.get("suite"), dict) or "tasks" not in report["suite"]:
        raise ValueError("runtime evidence requires a task snapshot")
    raw_options = runtime.get("options")
    if not isinstance(raw_options, dict) or set(raw_options) != set(asdict(RuntimeOptions())):
        raise ValueError("invalid runtime options")
    options = RuntimeOptions(**raw_options)

    def duration(value: Any) -> bool:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    elapsed = runtime.get("duration_seconds")
    rate = runtime.get("completed_tasks_per_second")
    peak = runtime.get("peak_in_flight")
    if (not duration(elapsed) or elapsed <= 0 or not duration(rate)
            or type(peak) is not int or not 1 <= peak <= min(options.concurrency, len(report["results"]))):
        raise ValueError("invalid runtime metrics")
    if not math.isclose(rate, len(report["results"]) / elapsed, rel_tol=1e-9):
        raise ValueError("runtime throughput does not match duration")
    total_attempts = 0
    for result in report["results"]:
        attempts = result.get("attempts")
        if (not duration(result.get("queue_seconds")) or not duration(result.get("duration_seconds"))
                or result["queue_seconds"] + result["duration_seconds"] > elapsed
                or not isinstance(attempts, list) or not 1 <= len(attempts) <= options.max_attempts):
            raise ValueError("invalid runtime result")
        for index, attempt in enumerate(attempts):
            if (not isinstance(attempt, dict)
                    or set(attempt) != {"number", "duration_seconds", "error", "retryable"}
                    or type(attempt.get("number")) is not int
                    or attempt["number"] != index + 1 or not duration(attempt.get("duration_seconds"))
                    or type(attempt.get("retryable")) is not bool
                    or attempt.get("error") not in (None, "provider request failed")
                    or (attempt["retryable"] and attempt.get("error") is None)):
                raise ValueError("invalid runtime attempt")
            if index < len(attempts) - 1 and not attempt["retryable"]:
                raise ValueError("retry follows a non-retryable attempt")
        if (attempts[-1]["error"] != result["error"]
                or (result["error"] is not None and result.get("answer") is not None)
                or sum(attempt["duration_seconds"] for attempt in attempts) > result["duration_seconds"]
                or (attempts[-1]["retryable"] and len(attempts) != options.max_attempts)):
            raise ValueError("runtime attempts do not match result")
        total_attempts += len(attempts)
    if type(runtime.get("attempts")) is not int or runtime["attempts"] != total_attempts:
        raise ValueError("runtime attempt total does not match results")
