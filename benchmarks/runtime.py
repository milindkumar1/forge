"""Separate synthetic I/O experiments and explicitly opted-in real API measurements."""

import argparse
import asyncio
import platform
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from forge import __version__
from forge.cli import run_native
from forge.config import Config, load_config, write_json
from forge.evaluation import evaluate
from forge.generation import generate_suite
from forge.metrics import benchmark_metrics
from forge.native_runtime import NativeRuntimeOptions, evaluate_native
from forge.providers import AsyncMockProvider, MockProvider, RetryableProviderError
from forge.runtime import RuntimeOptions, evaluate_async


class _Failures:
    def __init__(self, prompts: List[str], every: int) -> None:
        self.fail = set(prompts[::every]) if every else set()
        self.lock = threading.Lock()

    def check(self, prompt: str) -> None:
        with self.lock:
            fail = prompt in self.fail
            self.fail.discard(prompt)
        if fail:
            raise RetryableProviderError("injected synthetic transient failure")


class DelayedMock(MockProvider):
    def __init__(self, delay: float, failures: _Failures) -> None:
        super().__init__()
        self.delay, self.failures = delay, failures

    def complete(self, prompt: str) -> str:
        time.sleep(self.delay)
        self.failures.check(prompt)
        return super().complete(prompt)


class DelayedAsyncMock(AsyncMockProvider):
    def __init__(self, delay: float, failures: _Failures) -> None:
        super().__init__()
        self.delay, self.failures = delay, failures

    async def acomplete(self, prompt: str) -> str:
        await asyncio.sleep(self.delay)
        self.failures.check(prompt)
        return await super().acomplete(prompt)


def synthetic_experiment(count: int, repeats: int, delay: float, transient_every: int) -> Dict[str, Any]:
    suite = generate_suite(seed=42, count=count)
    prompts = [task.prompt for task in suite.tasks]
    modes = [("sequential", 1), ("thread", 1), ("thread", 4), ("native", 1), ("native", 4)]
    # Unmeasured warmups are offline only.
    evaluate(Config(), MockProvider(), suite)
    asyncio.run(evaluate_async(Config(), MockProvider(), suite, RuntimeOptions(4)))
    asyncio.run(evaluate_native(Config(), AsyncMockProvider(), suite, NativeRuntimeOptions(4)))
    samples = []
    for repeat in range(repeats):
        offset = repeat % len(modes)
        for kind, concurrency in modes[offset:] + modes[:offset]:
            failures = _Failures(prompts, transient_every)
            provider = DelayedMock(delay, failures)
            begin = time.perf_counter()
            if kind == "sequential":
                report = evaluate(Config(), provider, suite)
            elif kind == "thread":
                report = asyncio.run(evaluate_async(Config(), provider, suite,
                                    RuntimeOptions(concurrency, 2, 0)))
            else:
                report = asyncio.run(evaluate_native(Config(), DelayedAsyncMock(delay, failures), suite,
                                    NativeRuntimeOptions(concurrency, 2, 0)))
            elapsed = time.perf_counter() - begin
            if not transient_every and report["summary"]["passed"] != count:
                raise RuntimeError("benchmark correctness check failed")
            samples.append(dict(benchmark_metrics(report, elapsed), kind=kind,
                                concurrency=concurrency, repeat=repeat, summary=report["summary"]))
    medians = {kind + "-" + str(concurrency): statistics.median(
        sample["wall_seconds"] for sample in samples
        if sample["kind"] == kind and sample["concurrency"] == concurrency)
        for kind, concurrency in modes}
    return {"measurement_kind": "synthetic", "experiment": "synthetic-blocking-io-v2",
            "suite_sha256": suite.digest, "seed": 42, "count": count, "repeats": repeats,
            "delay_seconds": delay, "transient_every": transient_every,
            "policy": "one attempt sequential; two attempts and zero backoff for concurrent modes",
            "samples": samples, "median_wall_seconds": medians}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("synthetic", "api"), default="synthetic")
    parser.add_argument("--count", type=int, default=24)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--delay", type=float, default=0.01, help="synthetic only")
    parser.add_argument("--transient-every", type=int, default=0, help="synthetic: fail first attempt of every Nth task")
    parser.add_argument("--config", type=Path, help="API mode: OpenRouter config")
    parser.add_argument("--allow-network", action="store_true", help="API mode: allow paid requests")
    parser.add_argument("--concurrency", type=int, default=4, help="API mode")
    parser.add_argument("--max-attempts", type=int, default=1, help="API mode")
    parser.add_argument("--request-timeout", type=float, default=30, help="API mode")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 100 or not 0 <= args.delay <= 1 or args.transient_every < 0:
        parser.error("repeats must be in [1, 100], delay finite in [0, 1], transient-every nonnegative")
    if args.output.exists():
        parser.error("output already exists")
    if not args.output.parent.is_dir():
        parser.error("output parent directory does not exist")
    if args.mode == "synthetic":
        if args.allow_network or args.config is not None:
            parser.error("network/config require --mode api; synthetic results never contain API measurements")
        if args.concurrency != 4 or args.max_attempts != 1 or args.request_timeout != 30:
            parser.error("concurrency/attempt/timeout flags require --mode api")
        result = synthetic_experiment(args.count, args.repeats, args.delay, args.transient_every)
    else:
        if not args.allow_network or args.config is None:
            parser.error("API measurements require --config and explicit --allow-network (may incur costs)")
        if args.transient_every or args.delay != 0.01:
            parser.error("synthetic delay/failure injection cannot be used with API measurements")
        config = load_config(args.config)
        if config.provider != "openrouter":
            parser.error("API mode requires an OpenRouter provider config")
        suite = generate_suite(seed=42, count=args.count)
        options = NativeRuntimeOptions(concurrency=args.concurrency, max_attempts=args.max_attempts,
                                       request_timeout_seconds=args.request_timeout)
        reports, samples = [], []
        # No paid warmups, injected delays/failures, or synthetic samples in API mode.
        for repeat in range(args.repeats):
            begin = time.perf_counter()
            report = asyncio.run(run_native(config, suite, options, True))
            samples.append(dict(benchmark_metrics(report, time.perf_counter() - begin), repeat=repeat))
            reports.append(report)
        result = {"measurement_kind": "real_api", "experiment": "real-api-native-v1",
                  "count": args.count, "repeats": args.repeats, "reports": reports, "samples": samples}
    result.update(schema_version=2, forge_version=__version__, python=platform.python_version(),
                  platform=platform.platform())
    write_json(args.output, result)
    print("Saved {} measurements to {}".format(result["measurement_kind"], args.output))
    if "median_wall_seconds" in result:
        print("Median wall seconds: " + str(result["median_wall_seconds"]))


if __name__ == "__main__":
    main()
