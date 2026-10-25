"""Offline I/O-overlap experiment; no model, API, or GPU performance claims."""

import argparse
import asyncio
import platform
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List

from forge import __version__
from forge.config import Config, write_json
from forge.evaluation import evaluate
from forge.generation import generate_suite
from forge.providers import MockProvider
from forge.runtime import RuntimeOptions, evaluate_async


class DelayedMock(MockProvider):
    """Simulate blocking I/O using a known sleep before trusted fixture work."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    def complete(self, prompt: str) -> str:
        time.sleep(self.delay)
        return super().complete(prompt)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=24)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--delay", type=float, default=0.01)
    args = parser.parse_args()
    if args.repeats < 1 or not 0 <= args.delay <= 1:
        parser.error("repeats must be positive and delay must be finite in [0, 1]")
    if args.output.exists():
        parser.error("output already exists")
    suite = generate_suite(seed=42, count=args.count)
    samples: List[Dict[str, Any]] = []
    # Warm up interpreter/executor paths; exclude these runs from samples.
    evaluate(Config(), MockProvider(), suite)
    asyncio.run(evaluate_async(Config(), MockProvider(), suite, RuntimeOptions(4)))
    for repeat in range(args.repeats):
        # Rotate order to reduce a fixed first-run bias.
        modes = [("sequential", 1), ("async", 1), ("async", 4)]
        modes = modes[repeat % 3:] + modes[:repeat % 3]
        for kind, concurrency in modes:
            provider = DelayedMock(args.delay)
            begin = time.perf_counter()
            report = (evaluate(Config(), provider, suite) if kind == "sequential" else
                      asyncio.run(evaluate_async(Config(), provider, suite, RuntimeOptions(concurrency))))
            elapsed = time.perf_counter() - begin
            if report["summary"]["passed"] != args.count:
                raise RuntimeError("benchmark correctness check failed")
            durations = sorted(result["duration_seconds"] for result in report["results"])
            samples.append({"kind": kind, "concurrency": concurrency, "repeat": repeat,
                            "wall_seconds": elapsed, "tasks_per_second": args.count / elapsed,
                            "task_duration_median_seconds": statistics.median(durations),
                            "task_duration_p95_seconds": durations[max(0, (95 * len(durations) + 99) // 100 - 1)],
                            "summary": report["summary"]})
    medians = {kind + "-" + str(concurrency): statistics.median(
        sample["wall_seconds"] for sample in samples
        if sample["kind"] == kind and sample["concurrency"] == concurrency)
        for kind, concurrency in (("sequential", 1), ("async", 1), ("async", 4))}
    result = {"schema_version": 1, "experiment": "synthetic-blocking-io-v1",
              "forge_version": __version__, "python": platform.python_version(),
              "platform": platform.platform(), "suite_sha256": suite.digest,
              "seed": 42, "count": args.count, "repeats": args.repeats,
              "delay_seconds": args.delay, "samples": samples, "median_wall_seconds": medians,
              "synthetic_overlap_ratio": medians["sequential-1"] / medians["async-4"]}
    write_json(args.output, result)
    print("Median wall seconds: " + str(medians))
    print("Synthetic overlap ratio: {:.2f}x".format(result["synthetic_overlap_ratio"]))


if __name__ == "__main__":
    main()
