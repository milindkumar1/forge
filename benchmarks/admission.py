"""Synthetic admission-control comparison using accelerated monotonic time.

One simulated minute takes 60 / scale real seconds. These are admission-model
statistics, not real model/GPU latency or physical inference throughput.
"""

import asyncio
import math
from dataclasses import asdict
from time import monotonic, perf_counter
from typing import Any, Dict

from forge.admission import AdmissionOptions, Clock, ProviderCoordinator
from forge.config import Config
from forge.generation import generate_suite
from forge.metrics import benchmark_metrics
from forge.native_runtime import NativeRuntimeOptions, evaluate_native
from forge.providers import AsyncMockProvider, Completion, ProviderRateLimitError, RetryableProviderError


class ScaledClock:
    def __init__(self, scale: float) -> None:
        if type(scale) not in (int, float) or not math.isfinite(scale) or not 0 < scale <= 60000:
            raise ValueError("simulation scale must be finite in (0, 60000]")
        self.scale, self.origin = scale, monotonic()

    def now(self) -> float:
        return (monotonic() - self.origin) * self.scale

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / self.scale)


class LimitedMock(AsyncMockProvider):
    """Only accepted requests consume simulated server RPM/TPM capacity."""

    def __init__(self, limits: AdmissionOptions, clock: Clock, tokens: int, scenario: str) -> None:
        super().__init__()
        self.server = ProviderCoordinator(limits, clock=clock)
        self.tokens, self.scenario = tokens, scenario
        self.calls = self.rejections = 0
        self.failed_prompts = set()

    async def acomplete(self, prompt: str) -> str:
        return (await self.acomplete_with_usage(prompt)).text

    async def acomplete_with_usage(self, prompt: str) -> Completion:
        self.calls += 1
        ticket = self.server.enqueue(self.tokens, retry=False)
        assert ticket is not None
        decision = self.server.decide(ticket)
        if decision.permit is None:
            self.server.cancel(ticket)
            self.rejections += 1
            raise ProviderRateLimitError("synthetic provider quota", retry_after_seconds=decision.delay or 60.0)
        self.server.complete(decision.permit, actual_tokens=self.tokens)
        if self.scenario == "outage" or (self.scenario == "transient" and prompt not in self.failed_prompts):
            self.failed_prompts.add(prompt)
            raise RetryableProviderError("synthetic provider failure")
        return Completion(await super().acomplete(prompt), self.tokens)


async def limits_experiment(*, count: int = 24, repeats: int = 3, rpm: int = 4, tpm: int = 12,
                            tokens: int = 3, scale: float = 600, scenario: str = "clean",
                            concurrency: int = 4, retries_per_minute: int = 4,
                            failure_threshold: int = 5) -> Dict[str, Any]:
    if type(repeats) is not int or not 1 <= repeats <= 100:
        raise ValueError("repeats must be in [1, 100]")
    if scenario not in ("clean", "transient", "outage"):
        raise ValueError("unknown simulated scenario")
    suite = generate_suite(seed=42, count=count)
    policy = AdmissionOptions(requests_per_minute=rpm, tokens_per_minute=tpm, tokens_per_request=tokens,
                              retries_per_minute=retries_per_minute, failure_threshold=failure_threshold,
                              admission_timeout_seconds=3600)
    server_limits = AdmissionOptions(requests_per_minute=rpm, tokens_per_minute=tpm, failure_threshold=0)
    samples = []
    for repeat in range(repeats):
        modes = (False, True) if repeat % 2 == 0 else (True, False)
        for coordinated in modes:
            clock = ScaledClock(scale)
            provider = LimitedMock(server_limits, clock, tokens, scenario)
            coordinator = ProviderCoordinator(policy, clock=clock) if coordinated else None
            begin = perf_counter()
            report = await evaluate_native(Config(), provider, suite, NativeRuntimeOptions(
                concurrency=concurrency, max_attempts=3, retry_delay_seconds=0), coordinator=coordinator, clock=clock)
            wall_seconds = perf_counter() - begin
            logical_elapsed = report["runtime"]["duration_seconds"]
            metrics = benchmark_metrics(report, logical_elapsed)
            samples.append({"coordinated": coordinated, "repeat": repeat,
                            "simulation_seconds": logical_elapsed, "real_wall_seconds": wall_seconds,
                            "provider_calls": provider.calls, "provider_rejections": provider.rejections,
                            "metrics": metrics, "report": report})
    return {"measurement_kind": "synthetic", "experiment": "simulated-provider-admission-v1",
            "metrics_timebase": "simulated_seconds", "simulation_scale": scale,
            "server_model": "rolling 60s RPM/TPM; instantaneous service; only accepted attempts consume capacity",
            "scenario": scenario, "count": count, "repeats": repeats, "seed": 42,
            "suite_sha256": suite.digest, "client_policy": asdict(policy),
            "server_limits": asdict(server_limits), "samples": samples}
