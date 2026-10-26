"""Optional, bounded telemetry collection for local inference sweeps."""

import asyncio
import csv
import math
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from forge.config import LocalConfig

MAX_METRICS_BYTES = 2_000_000
MAX_GPU_SAMPLES = 10_000
SERVER_METRICS = {
    "vllm:num_requests_running", "vllm:num_requests_waiting", "vllm:kv_cache_usage_perc",
    "vllm:request_success", "vllm:prompt_tokens", "vllm:generation_tokens",
    "vllm:time_to_first_token_seconds", "vllm:inter_token_latency_seconds",
    "vllm:e2e_request_latency_seconds", "vllm:request_queue_time_seconds",
}
_SAMPLE_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([^\s]+)(?:\s+\d+)?$")


def metrics_url(config: LocalConfig) -> str:
    parsed = urlsplit(config.base_url)
    host = "[{}]".format(parsed.hostname) if ":" in parsed.hostname else parsed.hostname
    netloc = host + ((":" + str(parsed.port)) if parsed.port is not None else "")
    return urlunsplit((parsed.scheme, netloc, "/metrics", "", ""))


def parse_server_metrics(payload: str) -> Dict[str, List[Dict[str, Any]]]:
    """Parse selected Prometheus samples while preserving labels and finite values."""
    output: Dict[str, List[Dict[str, Any]]] = {}
    for line in payload.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_LINE.fullmatch(line.strip())
        if match is None:
            continue
        name, labels, raw_value = match.groups()
        root = name.rsplit("_", 1)[0] if name.endswith(("_sum", "_count", "_bucket")) else name
        if root not in SERVER_METRICS:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        output.setdefault(name, []).append({"labels": labels or "", "value": value})
    return output


async def scrape_server_metrics(config: LocalConfig, *, timeout_seconds: float = 2.0) -> Dict[str, Any]:
    """Read the optional vLLM Prometheus endpoint; unsupported servers remain usable."""
    try:
        import aiohttp
    except ImportError:
        return {"available": False, "reason": "http_extra_unavailable", "samples": {}}
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False,
                                         cookie_jar=aiohttp.DummyCookieJar()) as session:
            async with session.get(metrics_url(config), allow_redirects=False) as response:
                if response.status != 200:
                    return {"available": False, "reason": "http_{}".format(response.status), "samples": {}}
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_METRICS_BYTES:
                        return {"available": False, "reason": "response_too_large", "samples": {}}
        parsed = parse_server_metrics(body.decode("utf-8"))
        if not parsed:
            return {"available": False, "reason": "no_supported_metrics", "samples": {}}
        return {"available": True, "reason": None, "samples": parsed}
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError, OSError):
        return {"available": False, "reason": "metrics_unavailable", "samples": {}}


def parse_nvidia_smi_row(line: str) -> Optional[Dict[str, Any]]:
    """Parse one CSV row from the fixed nvidia-smi query; N/A fields stay null."""
    try:
        fields = next(csv.reader([line], skipinitialspace=True))
        if len(fields) != 7:
            return None
        index = int(fields[0].strip())
        values = [field.strip() for field in fields[2:]]

        def quantity(value: str, suffix: str) -> Optional[float]:
            if value.upper() == "N/A":
                return None
            cleaned = value.removesuffix(suffix).strip()
            number = float(cleaned)
            return number if math.isfinite(number) and number >= 0 else None

        return {"device_index": index, "captured_at": fields[1].strip(),
                "gpu_utilization_percent": quantity(values[0], "%"),
                "memory_utilization_percent": quantity(values[1], "%"),
                "memory_used_mib": quantity(values[2], "MiB"),
                "memory_total_mib": quantity(values[3], "MiB"),
                "power_watts": quantity(values[4], "W")}
    except (StopIteration, ValueError, csv.Error):
        return None


class NvidiaSmiSampler:
    """One-second GPU sampler with fixed argv, bounded rows, and awaited cleanup."""

    def __init__(self) -> None:
        self.process: Any = None
        self.reader: Optional[asyncio.Task] = None
        self.samples: List[Dict[str, Any]] = []
        self.reason: Optional[str] = None

    async def start(self) -> None:
        try:
            self.process = await asyncio.create_subprocess_exec(
                "nvidia-smi", "--query-gpu=index,timestamp,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw",
                "--format=csv,noheader", "-l", "1", stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
        except (FileNotFoundError, PermissionError, OSError):
            self.reason = "nvidia_smi_unavailable"
            return
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        while len(self.samples) < MAX_GPU_SAMPLES:
            line = await self.process.stdout.readline()
            if not line:
                return
            sample = parse_nvidia_smi_row(line.decode("utf-8", errors="replace").strip())
            if sample is not None:
                self.samples.append(sample)

    async def close(self) -> Dict[str, Any]:
        if self.process is not None and self.process.returncode is None:
            try:
                self.process.terminate()
            except OSError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    self.process.kill()
                    await self.process.wait()
                except OSError:
                    self.reason = self.reason or "nvidia_smi_cleanup_failed"
        if self.reader is not None:
            try:
                await asyncio.wait_for(self.reader, timeout=2.0)
            except asyncio.TimeoutError:
                self.reader.cancel()
                await asyncio.gather(self.reader, return_exceptions=True)
            except OSError:
                self.reason = self.reason or "nvidia_smi_read_failed"
        if self.process is not None and not self.samples and self.reason is None:
            self.reason = "nvidia_smi_no_samples"
        return {"available": bool(self.samples), "reason": self.reason,
                "sample_interval_seconds": 1, "samples": self.samples}
