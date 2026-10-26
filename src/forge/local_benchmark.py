"""Reproducible loopback endpoint sweeps; no server provisioning or GPU claims."""

import argparse
import asyncio
import hashlib
import platform
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, Tuple

from forge import __version__
from forge.config import LocalConfig, load_config, validate_local_config, write_json
from forge.inference import parse_inference_info
from forge.inference_runtime import run_native
from forge.metrics import benchmark_metrics, latency_percentiles
from forge.native_runtime import NativeRuntimeOptions
from forge.suites import DEFAULT_SUITE, TaskSuite, load_suite, validate_suite
from forge.telemetry import NvidiaSmiSampler, scrape_server_metrics


@dataclass(frozen=True)
class SweepOptions:
    concurrencies: Tuple[int, ...] = (1, 2, 4)
    repeats: int = 3
    request_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if (not isinstance(self.concurrencies, tuple) or not 1 <= len(self.concurrencies) <= 16
                or any(type(c) is not int or not 1 <= c <= 64 for c in self.concurrencies)
                or len(set(self.concurrencies)) != len(self.concurrencies)
                or type(self.repeats) is not int or not 1 <= self.repeats <= 100):
            raise ValueError("sweep needs 1-16 unique concurrency values in [1, 64] and 1-100 repeats")
        NativeRuntimeOptions(request_timeout_seconds=self.request_timeout_seconds)


def endpoint_metrics(report: Dict[str, Any], elapsed: float) -> Dict[str, Any]:
    metrics = benchmark_metrics(report, elapsed)
    successful = [a for result in report["results"] for a in result["attempts"] if a["error"] is None]
    infos = [parse_inference_info(a["inference"], a["reported_tokens"]) if "inference" in a else None
             for a in successful]
    outputs = [info.completion_tokens for info in infos if info is not None and info.completion_tokens is not None]
    traces = [info.stream for info in infos if info is not None and info.stream is not None]
    firsts = [trace.content_arrival_seconds[0] for trace in traces if trace.content_arrival_seconds]
    gaps = [right - left for trace in traces
            for left, right in zip(trace.content_arrival_seconds, trace.content_arrival_seconds[1:])]
    metrics.update(
        completed_responses=len(successful), usage_covered_responses=len(outputs),
        output_usage_coverage=len(outputs) / len(successful) if successful else None,
        known_output_tokens=sum(outputs),
        completed_response_output_tokens_per_second=sum(outputs) / elapsed
            if successful and len(outputs) == len(successful) else None,
        streamed_responses=len(traces), responses_with_content=len(firsts),
        content_delta_count=sum(len(trace.content_arrival_seconds) for trace in traces),
        first_content_seconds=latency_percentiles(firsts) if firsts else None,
        inter_content_delta_seconds=latency_percentiles(gaps) if gaps else None,
        length_limited_responses=sum(trace.finish_reason == "length" for trace in traces),
    )
    return metrics


async def benchmark_local(config: LocalConfig, suite: TaskSuite = DEFAULT_SUITE,
                          options: SweepOptions = SweepOptions(), *, allow_network: bool = False,
                          collect_telemetry: bool = False) -> Dict[str, Any]:
    if not isinstance(config, LocalConfig):
        raise ValueError("local benchmark requires schema-2 local endpoint configuration")
    validate_local_config(config)
    validate_suite(suite).require_valid()
    calls = len(suite.tasks) * len(options.concurrencies) * options.repeats
    if calls > 10000:
        raise ValueError("sweep exceeds the 10000-request evidence limit")
    if not allow_network:
        raise ValueError("local benchmark requires explicit --allow-network")
    try:
        http_version = version("aiohttp")
    except PackageNotFoundError as exc:
        raise ValueError("native HTTP requires: pip install 'forge-evals[http]'") from exc
    # Collect client provenance before issuing any inference requests.
    client = {"forge_version": __version__, "python": platform.python_version(),
              "platform": platform.platform(), "aiohttp": http_version,
              "source_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in sorted(Path(__file__).parent.glob("*.py"))}}
    started = datetime.now(timezone.utc).isoformat()
    samples = []
    sampler = NvidiaSmiSampler() if collect_telemetry else None
    server_before = server_after = None
    gpu_telemetry = None
    if sampler is not None:
        await sampler.start()
    try:
        if sampler is not None:
            server_before = await scrape_server_metrics(config)
        for repeat in range(options.repeats):
            offset = repeat % len(options.concurrencies)
            order = options.concurrencies[offset:] + options.concurrencies[:offset]
            for concurrency in order:
                begin = perf_counter()
                report = await run_native(config, suite, NativeRuntimeOptions(
                    concurrency=concurrency, max_attempts=1,
                    request_timeout_seconds=options.request_timeout_seconds), True)
                elapsed = perf_counter() - begin
                samples.append({"repeat": repeat, "concurrency": concurrency,
                                "metrics": endpoint_metrics(report, elapsed), "report": report})
    finally:
        if sampler is not None:
            gpu_telemetry = await sampler.close()
            server_after = await scrape_server_metrics(config)
    return {"schema_version": 1, "kind": "local-inference-benchmark", "measurement_kind": "endpoint",
            "started_at": started, "client": client, "config": config.to_dict(), "sweep": asdict(options),
            "suite_sha256": suite.digest, "provenance_source": "operator_declared",
            "server_revision_verified": False, "token_counts_source": "provider_reported",
            "generation": {"seed": 0, "temperature": 0, "max_tokens": config.max_tokens, "stream": config.stream},
            "policy": {"max_attempts": 1, "rpm": None, "tpm": None, "breaker": False,
                       "warmup_requests": 0, "connection_scope": "new_session_per_sample", "sample_order": "rotated"},
            "planned_requests": calls, "gpu_telemetry": gpu_telemetry,
            "server_metrics": ({"before": server_before, "after": server_after}
                               if collect_telemetry else None), "samples": samples}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--request-timeout", type=float, default=30)
    parser.add_argument("--allow-network", action="store_true", help="connect to the configured existing local server")
    parser.add_argument("--collect-telemetry", action="store_true",
                        help="sample nvidia-smi and scrape optional local vLLM metrics")
    args = parser.parse_args()
    try:
        if args.output.exists() or not args.output.parent.is_dir():
            raise ValueError("choose a new output path in an existing directory")
        options = SweepOptions(tuple(args.concurrency), args.repeats, args.request_timeout)
        config = load_config(args.config)
        suite = load_suite(args.tasks) if args.tasks is not None else DEFAULT_SUITE
        result = asyncio.run(benchmark_local(config, suite, options, allow_network=args.allow_network,
                                             collect_telemetry=args.collect_telemetry))
        write_json(args.output, result)
        telemetry = "with telemetry" if args.collect_telemetry else "without telemetry"
        print("Saved {} endpoint samples to {} ({})".format(len(result["samples"]), args.output, telemetry))
        return 0
    except (OSError, ValueError) as exc:
        print("forge local benchmark: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
