import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.admission import LimitedMock, ScaledClock, limits_experiment
from benchmarks.runtime import main
from forge.admission import AdmissionOptions
from forge.admission_reports import validate_coordinated_report
from forge.providers import ProviderRateLimitError
from test_admission import ManualClock


class AdmissionBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_simulated_provider_enforces_exact_window_and_usage(self):
        clock = ManualClock()
        server = LimitedMock(AdmissionOptions(requests_per_minute=2, tokens_per_minute=6), clock, 3, "clean")
        prompt = '{"operation":"reverse","text":"forge"}'
        self.assertEqual((await server.acomplete_with_usage(prompt)).total_tokens, 3)
        await server.acomplete(prompt)
        with self.assertRaises(ProviderRateLimitError) as caught:
            await server.acomplete(prompt)
        self.assertEqual(caught.exception.retry_after_seconds, 60)
        self.assertEqual(server.rejections, 1)
        clock.advance(60)
        self.assertEqual(await server.acomplete(prompt), "egrof")

    async def test_limits_experiment_is_synthetic_and_reports_admission_costs(self):
        report = await limits_experiment(count=6, repeats=1, rpm=3, tpm=9)
        self.assertEqual(report["measurement_kind"], "synthetic")
        self.assertEqual(report["metrics_timebase"], "simulated_seconds")
        self.assertEqual(len(report["samples"]), 2)
        for sample in report["samples"]:
            metrics = sample["metrics"]
            self.assertEqual(metrics["peak_admission_queue"], 6)
            self.assertIsNotNone(metrics["admission_latency_seconds"])
            self.assertGreaterEqual(metrics["mean_admission_queue"], 0)
            self.assertGreater(sample["real_wall_seconds"], 0)
            if sample["coordinated"]:
                self.assertEqual(sample["provider_rejections"], 0)
                self.assertEqual(metrics["admission_rejections"], 0)
                self.assertEqual(sample["report"]["summary"]["passed"], 6)
                validate_coordinated_report(sample["report"])


class AdmissionBenchmarkCliTests(unittest.TestCase):
    def test_cli_simulation_output_and_mode_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "limits.json"
            argv = ["benchmark", "--mode", "limits", "--count", "3", "--repeats", "1", "--output", str(output)]
            with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
                main()
            self.assertEqual(json.loads(output.read_text())["measurement_kind"], "synthetic")
            for flags in (["--mode", "limits", "--allow-network"], ["--mode", "api", "--simulated-rpm", "2"],
                          ["--simulated-tpm", "10"]):
                with patch("sys.argv", ["benchmark", "--output", str(Path(directory) / "bad.json")] + flags), \
                     contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main()
                self.assertEqual(caught.exception.code, 2)

    def test_scaled_clock_rejects_invalid_scaling(self):
        for scale in (0, -1, True, float("nan"), float("inf"), 60001):
            with self.assertRaises(ValueError):
                ScaledClock(scale)


if __name__ == "__main__":
    unittest.main()
