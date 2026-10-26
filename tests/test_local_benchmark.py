import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forge.config import Config, write_json
from forge.evaluation import load_report
from forge.local_benchmark import SweepOptions, benchmark_local, endpoint_metrics, main
from local_http_fixture import LocalChatFixture
from test_local_provider import config


class SweepContractTests(unittest.TestCase):
    def test_invalid_options_and_cli_preflight_make_no_requests(self):
        for fields in ({"concurrencies": ()}, {"concurrencies": (1, 1)}, {"concurrencies": (True,)},
                       {"concurrencies": (65,)}, {"repeats": 0}, {"request_timeout_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                SweepOptions(**fields)
        with tempfile.TemporaryDirectory() as directory:
            path, output = Path(directory) / "config.json", Path(directory) / "output.json"
            write_json(path, config().to_dict())
            argv = ["benchmark", "--config", str(path), "--output", str(output)]
            with patch("sys.argv", argv), patch("forge.local_benchmark.run_native") as run, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(), 2)  # no network flag
                run.assert_not_called()
            write_json(output, {"preserve": True})
            before = output.read_bytes()
            with patch("sys.argv", argv + ["--allow-network"]), patch("forge.local_benchmark.run_native") as run, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(), 2)
                run.assert_not_called()
            self.assertEqual(output.read_bytes(), before)

    def test_unknown_usage_and_single_delta_do_not_fabricate_token_or_gap_metrics(self):
        attempt = {"error": None, "duration_seconds": 1, "reported_tokens": None,
                   "inference": {"prompt_tokens": None, "completion_tokens": None, "response_model": "fixture",
                     "stream": {"timing_unit": "content_delta", "content_arrival_seconds": [0.25],
                                "duration_seconds": 0.5, "finish_reason": "stop"}}}
        report = {"results": [{"duration_seconds": 1, "attempts": [attempt]}],
                  "summary": {"passed": 1, "errors": 0, "failed": 0}}
        metrics = endpoint_metrics(report, 2)
        self.assertEqual(metrics["first_content_seconds"]["p50"], 0.25)
        self.assertIsNone(metrics["inter_content_delta_seconds"])
        self.assertIsNone(metrics["completed_response_output_tokens_per_second"])
        self.assertEqual(metrics["output_usage_coverage"], 0)
        attempt["reported_tokens"] = 7
        attempt["inference"].update(prompt_tokens=5, completion_tokens=2)
        metrics = endpoint_metrics(report, 2)
        self.assertEqual(metrics["completed_response_output_tokens_per_second"], 1)
        self.assertEqual(metrics["content_delta_count"], 1)  # two reported tokens, one delta


@unittest.skipIf(importlib.util.find_spec("aiohttp") is None, "install the http extra")
class LocalSweepTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_sweep_records_provenance_order_usage_and_raw_reports(self):
        async with LocalChatFixture() as fixture:
            result = await benchmark_local(fixture.config(stream=True), options=SweepOptions((1, 2), 2), allow_network=True)
            self.assertEqual(len(fixture.requests), 24)
            self.assertEqual(result["planned_requests"], 24)
            self.assertEqual(result["measurement_kind"], "endpoint")
            self.assertFalse(result["server_revision_verified"])
            self.assertIsNone(result["gpu_telemetry"])
            self.assertEqual([s["concurrency"] for s in result["samples"]], [1, 2, 2, 1])
            self.assertIn("streaming.py", result["client"]["source_sha256"])
            with tempfile.TemporaryDirectory() as directory:
                for index, sample in enumerate(result["samples"]):
                    self.assertEqual(sample["metrics"]["known_output_tokens"], 12)
                    self.assertEqual(sample["metrics"]["output_usage_coverage"], 1)
                    self.assertEqual(sample["metrics"]["streamed_responses"], 6)
                    self.assertEqual(sample["metrics"]["retry_count"], 0)
                    path = Path(directory) / str(index)
                    write_json(path, sample["report"])
                    self.assertEqual(load_report(path)["summary"]["passed"], 6)

    async def test_invalid_config_suite_size_or_consent_prevents_dispatch(self):
        from forge.generation import generate_suite

        cases = [(Config(), SweepOptions(), True), (config(), SweepOptions(), False),
                 (config(), SweepOptions((1, 2), 100), True)]
        for value, options, consent in cases:
            with patch("forge.local_benchmark.run_native") as run, self.assertRaises(ValueError):
                await benchmark_local(value, generate_suite(count=100), options, allow_network=consent)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
