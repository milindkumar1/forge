import asyncio
import contextlib
import copy
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from forge.cli import main
from forge.config import Config, write_json
from forge.evaluation import evaluate, load_report
from forge.providers import MockProvider, OpenRouterProvider, ProviderError, RetryableProviderError
from forge.runtime import RuntimeOptions, evaluate_async, validate_runtime_report
from forge.suites import DEFAULT_SUITE


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_bound_overlap_stable_order_and_sequential_equivalence(self):
        barrier = threading.Barrier(3, timeout=3)
        lock = threading.Lock()
        active = peak = calls = 0

        class BlockingMock(MockProvider):
            def complete(self, prompt):
                nonlocal active, peak, calls
                with lock:
                    active += 1
                    peak = max(active, peak)
                    calls += 1
                    number = calls
                try:
                    if number <= 3:
                        barrier.wait()  # Proves overlap without a timing threshold.
                    return super().complete(prompt)
                finally:
                    with lock:
                        active -= 1

        report = await evaluate_async(Config(), BlockingMock(), options=RuntimeOptions(3))
        baseline = evaluate(Config(), MockProvider())
        self.assertEqual(peak, 3)
        self.assertEqual(active, 0)
        self.assertEqual(calls, 6)
        self.assertEqual(report["summary"], baseline["summary"])
        self.assertEqual([r["task_id"] for r in report["results"]], [t.task_id for t in DEFAULT_SUITE.tasks])
        self.assertEqual([r["answer"] for r in report["results"]], [r["answer"] for r in baseline["results"]])
        validate_runtime_report(report)

    async def test_results_keep_input_order_when_completions_are_reversed(self):
        second_done = threading.Event()
        first_prompt = DEFAULT_SUITE.tasks[0].prompt
        second_prompt = DEFAULT_SUITE.tasks[1].prompt
        finished = []

        class Reordered(MockProvider):
            def complete(self, prompt):
                if prompt == first_prompt:
                    if not second_done.wait(3):
                        raise RuntimeError("second task did not overlap")
                answer = super().complete(prompt)
                finished.append(prompt)
                if prompt == second_prompt:
                    second_done.set()
                return answer

        report = await evaluate_async(Config(), Reordered(), options=RuntimeOptions(2))
        self.assertLess(finished.index(second_prompt), finished.index(first_prompt))
        self.assertEqual([r["prompt"] for r in report["results"]], [t.prompt for t in DEFAULT_SUITE.tasks])

    async def test_transient_retries_recover_with_bounded_exponential_backoff(self):
        counts = {}

        class Flaky(MockProvider):
            def complete(self, prompt):
                counts[prompt] = counts.get(prompt, 0) + 1
                if counts[prompt] < 3:
                    raise RetryableProviderError("secret")
                return super().complete(prompt)

        with patch("forge.runtime.asyncio.sleep", new_callable=unittest.mock.AsyncMock) as sleep:
            report = await evaluate_async(Config(), Flaky(), options=RuntimeOptions(1, 3, 0.1))
        self.assertEqual(report["summary"]["passed"], 6)
        self.assertEqual(report["runtime"]["attempts"], 18)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.1, 0.2] * 6)
        self.assertNotIn("secret", json.dumps(report))
        validate_runtime_report(report)

    async def test_exhaustion_permanent_failures_nontext_and_wrong_answers(self):
        class Broken:
            def __init__(self, failure):
                self.failure = failure

            def complete(self, prompt):
                if isinstance(self.failure, Exception):
                    raise self.failure
                return self.failure

        for failure, attempts, errors in ((RetryableProviderError("secret"), 2, 6),
                                          (ProviderError("secret"), 1, 6), (None, 1, 6), ("wrong", 1, 0)):
            with self.subTest(failure=type(failure).__name__):
                report = await evaluate_async(Config(), Broken(failure), options=RuntimeOptions(3, 2, 0))
                self.assertEqual(report["summary"]["failed"], 6)
                self.assertEqual(report["summary"]["errors"], errors)
                self.assertEqual(report["runtime"]["attempts"], attempts * 6)
                self.assertNotIn("secret", json.dumps(report))
                validate_runtime_report(report)

    async def test_invalid_suite_prevents_inference(self):
        provider = unittest.mock.Mock()
        suite = replace(DEFAULT_SUITE, tasks=(replace(DEFAULT_SUITE.tasks[0], expected="bad"),))
        with self.assertRaisesRegex(ValueError, "suite validation failed"):
            await evaluate_async(Config(), provider, suite)
        provider.complete.assert_not_called()

    async def test_worker_count_is_bounded_by_suite_size_and_loop_stays_responsive(self):
        started = threading.Event()
        release = threading.Event()

        class Paused(MockProvider):
            def complete(self, prompt):
                started.set()
                if not release.wait(3):
                    raise RuntimeError("event loop failed to release provider")
                return super().complete(prompt)

        job = asyncio.create_task(evaluate_async(Config(), Paused(), options=RuntimeOptions(64)))
        try:
            while not started.is_set():
                await asyncio.sleep(0.001)
            release.set()  # The event loop runs while provider threads are blocked.
            report = await job
        finally:
            release.set()
        self.assertLessEqual(report["runtime"]["peak_in_flight"], 6)
        validate_runtime_report(report)

    async def test_programming_errors_propagate_and_join_running_calls(self):
        barrier = threading.Barrier(2, timeout=3)
        finished = threading.Event()

        class Buggy:
            def complete(self, prompt):
                barrier.wait()
                if prompt == DEFAULT_SUITE.tasks[0].prompt:
                    raise RuntimeError("programming error")
                time.sleep(0.02)
                finished.set()
                return "wrong"

        with self.assertRaisesRegex(RuntimeError, "programming error"):
            await evaluate_async(Config(), Buggy(), options=RuntimeOptions(2))
        self.assertTrue(finished.is_set())
        self.assertFalse(any(t.name.startswith("forge-inference") for t in threading.enumerate()))

    async def test_cancellation_stops_admission_and_joins_inflight_calls(self):
        started = threading.Event()
        finished = threading.Event()
        calls = []

        class Slow:
            def complete(self, prompt):
                calls.append(prompt)
                started.set()
                time.sleep(0.05)
                finished.set()
                return "wrong"

        job = asyncio.create_task(evaluate_async(Config(), Slow()))
        while not started.is_set():
            await asyncio.sleep(0.001)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertTrue(finished.is_set())
        self.assertEqual(len(calls), 1)
        self.assertFalse(any(t.name.startswith("forge-inference") for t in threading.enumerate()))


class RuntimeContractTests(unittest.TestCase):
    def test_options_reject_invalid_types_ranges_and_nonfinite_values(self):
        for fields in ({"concurrency": True}, {"concurrency": 0}, {"concurrency": 65},
                       {"max_attempts": 0}, {"max_attempts": 6}, {"max_attempts": 1.5},
                       {"retry_delay_seconds": True}, {"retry_delay_seconds": -1},
                       {"retry_delay_seconds": 61}, {"retry_delay_seconds": float("nan")},
                       {"retry_delay_seconds": float("inf")}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                RuntimeOptions(**fields)

    def test_report_roundtrip_and_corrupt_runtime_evidence_rejection(self):
        original = asyncio.run(evaluate_async(Config(), MockProvider(), options=RuntimeOptions(4)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            write_json(path, original)
            self.assertEqual(load_report(path), original)
            for mutation in ("peak", "attempts", "rate", "duration", "options", "result", "attempt", "retry", "missing-error"):
                report = copy.deepcopy(original)
                if mutation == "peak":
                    report["runtime"]["peak_in_flight"] = 5
                elif mutation == "attempts":
                    report["runtime"]["attempts"] = 7
                elif mutation == "rate":
                    report["runtime"]["completed_tasks_per_second"] *= 2
                elif mutation == "duration":
                    report["runtime"]["duration_seconds"] = float("nan")
                elif mutation == "options":
                    report["runtime"]["options"]["max_attempts"] = True
                elif mutation == "result":
                    report["results"][0]["queue_seconds"] = -1
                elif mutation == "attempt":
                    report["results"][0]["attempts"] = []
                elif mutation == "retry":
                    report["results"][0]["attempts"][0]["retryable"] = True
                else:
                    del report["results"][0]["attempts"][0]["error"]
                path.write_text(json.dumps(report))
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    load_report(path)

    def test_cli_offline_run_report_preflight_and_network_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            config, output = Path(directory) / "config.json", Path(directory) / "report.json"
            write_json(config, Config().to_dict())
            args = ["run", "--config", str(config), "--output", str(output), "--concurrency", "3"]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 0)
                self.assertEqual(main(["report", "--input", str(output)]), 0)
                self.assertEqual(load_report(output)["runtime"]["options"]["concurrency"], 3)
                with patch("forge.cli.make_provider") as factory:
                    self.assertEqual(main(args), 2)
                    factory.assert_not_called()
                output.unlink()
                for extra in (["--concurrency", "0"], ["--max-attempts", "2"],
                              ["--concurrency", "2", "--retry-delay", "nan"]):
                    with patch("forge.cli.make_provider") as factory:
                        self.assertEqual(main(args[:-2] + extra), 2)
                        factory.assert_not_called()
                        self.assertFalse(output.exists())
                network_config = Path(directory) / "network.json"
                write_json(network_config, replace(Config(), provider="openrouter", model="test/model").to_dict())
                with patch("urllib.request.urlopen") as transport:
                    self.assertEqual(main(["run", "--config", str(network_config), "--output", str(output),
                                           "--concurrency", "3"]), 2)
                    transport.assert_not_called()

    def test_openrouter_marks_only_selected_transport_failures_retryable(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fake"}):
            provider = OpenRouterProvider("test/model", allow_network=True)
        for status, expected in ((429, RetryableProviderError), (503, RetryableProviderError),
                                 (401, ProviderError), (400, ProviderError)):
            body = io.BytesIO(b"secret response")
            failure = urllib.error.HTTPError("https://example.invalid", status, "secret", {}, body)
            with patch("urllib.request.urlopen", side_effect=failure), self.assertRaises(ProviderError) as caught:
                provider.complete("prompt")
            self.assertIs(type(caught.exception), expected)
            self.assertNotIn("secret", str(caught.exception))
            self.assertTrue(body.closed)

    def test_openrouter_retry_flows_through_runtime_with_fake_transport(self):
        suite = replace(DEFAULT_SUITE, tasks=(DEFAULT_SUITE.tasks[0],))
        config = replace(Config(), provider="openrouter", model="test/model")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fake"}):
            provider = OpenRouterProvider(config.model, allow_network=True)
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "egrof"}}]
        }).encode()
        failure = urllib.error.HTTPError("https://example.invalid", 503, "secret", {}, io.BytesIO())
        with patch("urllib.request.urlopen", side_effect=[failure, response]) as transport:
            report = asyncio.run(evaluate_async(config, provider, suite, RuntimeOptions(2, 2, 0)))
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(report["summary"]["passed"], 1)
        self.assertEqual(report["runtime"]["attempts"], 2)
        validate_runtime_report(report)


if __name__ == "__main__":
    unittest.main()
