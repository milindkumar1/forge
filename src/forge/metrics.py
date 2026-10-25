"""Explicit nearest-rank latency statistics shared by benchmark modes."""

import math
from typing import Any, Dict, List


def latency_percentiles(values: List[float]) -> Dict[str, float]:
    if not values or any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("latencies must be nonempty, finite, and nonnegative")
    ordered = sorted(values)
    return {"p" + str(p): ordered[math.ceil(p * len(ordered) / 100) - 1] for p in (50, 95, 99)}


def benchmark_metrics(report: Dict[str, Any], elapsed: float) -> Dict[str, Any]:
    if not math.isfinite(elapsed) or elapsed <= 0:
        raise ValueError("benchmark elapsed time must be positive and finite")
    results = report["results"]
    attempts = [attempt for result in results for attempt in result.get("attempts", [result])]
    return {
        "wall_seconds": elapsed, "completed_tasks_per_second": len(results) / elapsed,
        "successful_tasks_per_second": report["summary"]["passed"] / elapsed,
        "task_latency_seconds": latency_percentiles([result["duration_seconds"] for result in results]),
        "attempt_latency_seconds": latency_percentiles([attempt["duration_seconds"] for attempt in attempts]) if attempts else None,
        "completion_latency_seconds": latency_percentiles([
            result["queue_seconds"] + result["duration_seconds"] for result in results
        ]) if all("queue_seconds" in result for result in results) else None,
        "task_error_rate": report["summary"]["errors"] / len(results),
        "task_failure_rate": report["summary"]["failed"] / len(results),
        "attempt_error_rate": sum(attempt["error"] is not None for attempt in attempts) / len(attempts) if attempts else None,
        "attempt_count": len(attempts), "retry_count": sum(max(0, len(result.get("attempts", [result])) - 1) for result in results),
        "admission_latency_seconds": latency_percentiles([result["admission_seconds"] for result in results])
            if all("admission_seconds" in result for result in results) else None,
        "peak_admission_queue": report.get("runtime", {}).get("peak_admission_queue"),
        "mean_admission_queue": report.get("runtime", {}).get("mean_admission_queue"),
        "admission_rejections": report.get("runtime", {}).get("admission_rejections"),
        "peak_in_flight": report.get("runtime", {}).get("peak_in_flight"),
        "peak_pending_retries": report.get("runtime", {}).get("peak_pending_retries"),
        "retry_rejections": report.get("runtime", {}).get("retry_rejections"),
    }
