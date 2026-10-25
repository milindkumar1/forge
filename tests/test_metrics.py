import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.runtime import main, synthetic_experiment
from forge.config import Config
from forge.metrics import benchmark_metrics, latency_percentiles
from forge.native_runtime import NativeRuntimeOptions, evaluate_native
from forge.providers import AsyncMockProvider, RetryableProviderError


class MetricTests(unittest.TestCase):
    def test_nearest_rank_percentiles_and_invalid_values(self):
        self.assertEqual(latency_percentiles(list(range(1, 101))), {"p50": 50, "p95": 95, "p99": 99})
        self.assertEqual(latency_percentiles([7]), {"p50": 7, "p95": 7, "p99": 7})
        self.assertEqual(latency_percentiles([3, 1, 2]), {"p50": 2, "p95": 3, "p99": 3})
        for values in ([], [-1], [float("nan")], [float("inf")]):
            with self.assertRaises(ValueError):
                latency_percentiles(values)

    def test_recovered_retry_is_visible_even_with_zero_final_errors(self):
        class OnceFlaky(AsyncMockProvider):
            def __init__(self):
                super().__init__()
                self.failed = False

            async def acomplete(self, prompt):
                if not self.failed:
                    self.failed = True
                    raise RetryableProviderError("transient")
                return await super().acomplete(prompt)

        report = asyncio.run(evaluate_native(Config(), OnceFlaky(), options=NativeRuntimeOptions(
            max_attempts=2, retry_delay_seconds=0)))
        metrics = benchmark_metrics(report, 2)
        self.assertEqual(metrics["completed_tasks_per_second"], 3)
        self.assertEqual(metrics["successful_tasks_per_second"], 3)
        self.assertEqual(metrics["task_error_rate"], 0)
        self.assertEqual(metrics["attempt_error_rate"], 1 / 7)
        self.assertEqual(metrics["retry_count"], 1)
        self.assertEqual(set(metrics["attempt_latency_seconds"]), {"p50", "p95", "p99"})

    def test_synthetic_experiment_preserves_baselines_and_retry_evidence(self):
        with patch("benchmarks.runtime.run_native") as real_provider:
            result = synthetic_experiment(3, 1, 0, 1)
            real_provider.assert_not_called()
        self.assertEqual(result["measurement_kind"], "synthetic")
        self.assertEqual(len(result["samples"]), 5)
        for sample in result["samples"]:
            if sample["kind"] == "sequential":
                self.assertEqual(sample["task_error_rate"], 1)
                self.assertEqual(sample["retry_count"], 0)
                self.assertIsNone(sample["completion_latency_seconds"])
            else:
                self.assertEqual(sample["task_error_rate"], 0)
                self.assertEqual(sample["retry_count"], 3)
                self.assertEqual(sample["attempt_error_rate"], 0.5)

    def test_api_mode_requires_explicit_opt_in_and_rejects_synthetic_options(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.json"
            for extra in (["--mode", "api"], ["--allow-network"],
                          ["--mode", "api", "--allow-network", "--config", "missing.json", "--delay", "0.1"]):
                with patch("sys.argv", ["benchmark", "--output", str(output)] + extra), patch(
                        "benchmarks.runtime.run_native") as api, contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        main()
                    self.assertEqual(caught.exception.code, 2)
                    api.assert_not_called()
                    self.assertFalse(output.exists())

    def test_benchmark_cli_writes_separate_synthetic_artifact_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "synthetic.json"
            argv = ["benchmark", "--output", str(output), "--count", "3", "--repeats", "1", "--delay", "0"]
            with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
                main()
            data = json.loads(output.read_text())
            self.assertEqual(data["measurement_kind"], "synthetic")
            self.assertNotIn("reports", data)
            before = output.read_bytes()
            with patch("sys.argv", argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main()
            self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
