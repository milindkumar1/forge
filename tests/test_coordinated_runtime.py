import asyncio
import contextlib
import copy
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from forge.admission import AdmissionOptions, ProviderCoordinator
from forge.admission_reports import validate_coordinated_report
from forge.config import Config, write_json
from forge.cli import main
from forge.metrics import benchmark_metrics
from forge.evaluation import load_report
from forge.native_runtime import NativeRuntimeOptions, evaluate_native
from forge.providers import AsyncMockProvider, Completion, ProviderRateLimitError, RetryableProviderError
from forge.suites import DEFAULT_SUITE
from test_admission import ManualClock


async def drain():
    # Drain a bounded number of callback turns; no wall-clock sleeps/thresholds.
    for _ in range(50):
        await asyncio.sleep(0)


class CoordinatedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_rpm_windows_do_not_occupy_active_slots(self):
        clock = ManualClock()
        c = ProviderCoordinator(AdmissionOptions(requests_per_minute=2), clock=clock)
        starts = []

        class Logged(AsyncMockProvider):
            async def acomplete(self, prompt):
                starts.append(clock.now())
                return await super().acomplete(prompt)

        job = asyncio.create_task(evaluate_native(Config(), Logged(), options=NativeRuntimeOptions(4), coordinator=c))
        try:
            await drain()
            self.assertEqual(starts, [0, 0])
            clock.advance(59)
            await drain()
            self.assertEqual(len(starts), 2)
            clock.advance(1)
            await drain()
            self.assertEqual(starts, [0, 0, 60, 60])
            clock.advance(60)
            report = await job
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        self.assertEqual(starts, [0, 0, 60, 60, 120, 120])
        self.assertEqual(report["runtime"]["peak_in_flight"], 2)
        self.assertEqual(report["runtime"]["mean_admission_queue"], 3)
        self.assertEqual(report["runtime"]["attempts"], 6)
        validate_coordinated_report(report)

    async def test_usage_reconciliation_unblocks_tpm_and_retries_charge_both_budgets(self):
        clock = ManualClock()
        c = ProviderCoordinator(AdmissionOptions(tokens_per_minute=10, tokens_per_request=6), clock=clock)
        starts = []

        class Usage(AsyncMockProvider):
            async def acomplete_with_usage(self, prompt):
                starts.append(clock.now())
                return Completion(await super().acomplete(prompt), 3)

        job = asyncio.create_task(evaluate_native(Config(), Usage(), options=NativeRuntimeOptions(4), coordinator=c))
        try:
            await drain()
            self.assertEqual(starts, [0, 0])
            clock.advance(60)
            await drain()
            self.assertEqual(starts, [0, 0, 60, 60])
            clock.advance(60)
            report = await job
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        self.assertEqual(report["runtime"]["reserved_tokens"], 36)
        self.assertEqual(report["runtime"]["charged_tokens"], 18)
        validate_coordinated_report(report)

    async def test_retry_storm_stops_at_global_retry_budget(self):
        clock = ManualClock()
        c = ProviderCoordinator(AdmissionOptions(retries_per_minute=1, failure_threshold=0,
                                                tokens_per_request=5), clock=clock)

        class Outage:
            async def acomplete(self, prompt):
                raise RetryableProviderError("secret")

        report = await evaluate_native(Config(), Outage(), options=NativeRuntimeOptions(
            concurrency=2, max_attempts=5, retry_delay_seconds=0), coordinator=c)
        self.assertEqual(report["runtime"]["attempts"], 7)
        self.assertEqual(report["runtime"]["retries"], 1)
        self.assertEqual(report["runtime"]["charged_tokens"], 35)
        self.assertEqual(report["runtime"]["admission_rejections"], 6)
        self.assertTrue(all(r["termination"] == "retry_budget_exhausted" for r in report["results"]))
        self.assertNotIn("secret", json.dumps(report))
        validate_coordinated_report(report)

    async def test_429_pauses_fresh_attempts_and_retries_provider_wide(self):
        clock = ManualClock()
        c = ProviderCoordinator(AdmissionOptions(failure_threshold=0), clock=clock)
        starts = []

        class Throttled(AsyncMockProvider):
            async def acomplete(self, prompt):
                starts.append((prompt, clock.now()))
                if len(starts) == 1:
                    raise ProviderRateLimitError("429", retry_after_seconds=10)
                return await super().acomplete(prompt)

        job = asyncio.create_task(evaluate_native(Config(), Throttled(), options=NativeRuntimeOptions(
            max_attempts=2, retry_delay_seconds=0), coordinator=c))
        try:
            await drain()
            self.assertEqual(len(starts), 1)
            clock.advance(9)
            await drain()
            self.assertEqual(len(starts), 1)
            clock.advance(1)
            report = await job
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        self.assertTrue(all(t >= 10 for _, t in starts[1:]))
        self.assertEqual(report["summary"]["passed"], 6)
        self.assertEqual(report["runtime"]["retries"], 1)
        validate_coordinated_report(report)

    async def test_shared_runs_receive_fifo_admission_without_starvation(self):
        clock = ManualClock()
        c = ProviderCoordinator(AdmissionOptions(requests_per_minute=1), clock=clock)
        suite = replace(DEFAULT_SUITE, tasks=DEFAULT_SUITE.tasks[:2])
        starts = []

        class Logged(AsyncMockProvider):
            def __init__(self, label):
                super().__init__()
                self.label = label

            async def acomplete(self, prompt):
                starts.append((self.label, clock.now()))
                return await super().acomplete(prompt)

        jobs = [asyncio.create_task(evaluate_native(Config(), Logged(label), suite, coordinator=c)) for label in ("a", "b")]
        try:
            await drain()
            for _ in range(3):
                clock.advance(60)
                await drain()
            reports = await asyncio.gather(*jobs)
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
        self.assertEqual(starts, [("a", 0), ("b", 60), ("a", 120), ("b", 180)])
        for report in reports:
            validate_coordinated_report(report)

    async def test_fresh_and_retry_fairness_survives_multiple_rate_windows(self):
        clock = ManualClock()
        c = ProviderCoordinator(AdmissionOptions(requests_per_minute=2, failure_threshold=0,
                                                admission_timeout_seconds=600), clock=clock)
        counts, starts = {}, []

        class Flaky(AsyncMockProvider):
            async def acomplete(self, prompt):
                index = next(i for i, task in enumerate(DEFAULT_SUITE.tasks) if task.prompt == prompt)
                counts[index] = counts.get(index, 0) + 1
                starts.append((index, counts[index], clock.now()))
                if counts[index] < 3:
                    raise RetryableProviderError("transient")
                return await super().acomplete(prompt)

        job = asyncio.create_task(evaluate_native(Config(), Flaky(), options=NativeRuntimeOptions(
            max_attempts=3, retry_delay_seconds=0), coordinator=c))
        try:
            await drain()
            for _ in range(8):
                clock.advance(60)
                await drain()
            report = await job
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        self.assertEqual([(i, n) for i, n, _ in starts[:8]],
                         [(0, 1), (0, 2), (1, 1), (0, 3), (2, 1), (1, 2), (3, 1), (2, 2)])
        self.assertTrue(all(sum(t == window for _, _, t in starts) == 2 for window in range(0, 481, 60)))
        self.assertEqual(report["summary"]["passed"], 6)
        self.assertEqual(c.requests, 18)
        validate_coordinated_report(report)

    async def test_cancelled_admission_wait_removes_ticket_and_preserves_consumed_quota(self):
        clock = ManualClock()
        c = ProviderCoordinator(AdmissionOptions(requests_per_minute=1), clock=clock)
        job = asyncio.create_task(evaluate_native(Config(), AsyncMockProvider(), coordinator=c))
        await drain()
        self.assertEqual(c.requests, 1)
        self.assertTrue(clock.sleepers)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertFalse(clock.sleepers)
        self.assertFalse(c._waiters)
        ticket = c.enqueue(1, retry=False)
        self.assertEqual(c.decide(ticket).delay, 60)

    async def test_ready_retry_waiting_on_quota_still_occupies_pending_retry_budget(self):
        clock = ManualClock()
        c = ProviderCoordinator(AdmissionOptions(requests_per_minute=2, failure_threshold=0), clock=clock)
        release = asyncio.Event()
        counts = {}

        class Mixed(AsyncMockProvider):
            async def acomplete(self, prompt):
                counts[prompt] = counts.get(prompt, 0) + 1
                if counts[prompt] == 1 and prompt in [t.prompt for t in DEFAULT_SUITE.tasks[:2]]:
                    if prompt == DEFAULT_SUITE.tasks[1].prompt:
                        await release.wait()
                    raise RetryableProviderError("transient")
                return await super().acomplete(prompt)

        job = asyncio.create_task(evaluate_native(Config(), Mixed(), options=NativeRuntimeOptions(
            concurrency=2, max_attempts=2, retry_delay_seconds=0, max_pending_retries=1), coordinator=c))
        try:
            await drain()
            self.assertTrue(c._waiters[0].retry)
            release.set()
            await drain()
            for _ in range(3):
                clock.advance(60)
                await drain()
            report = await job
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        self.assertEqual(report["results"][1]["termination"], "retry_queue_full")
        self.assertEqual(report["runtime"]["peak_pending_retries"], 1)
        self.assertEqual(report["runtime"]["retries"], 1)
        validate_coordinated_report(report)

    async def test_cancellation_settles_active_reservations_without_refunding_unknown_usage(self):
        entered = asyncio.Queue()
        c = ProviderCoordinator(AdmissionOptions(tokens_per_minute=100, tokens_per_request=10))

        class Blocked:
            async def acomplete(self, prompt):
                entered.put_nowait(prompt)
                await asyncio.Event().wait()

        job = asyncio.create_task(evaluate_native(Config(), Blocked(), options=NativeRuntimeOptions(2), coordinator=c))
        await entered.get()
        await entered.get()
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertEqual(c.requests, 2)
        self.assertEqual(c.charged_tokens, 20)
        self.assertTrue(all(permit.settled for permit in c._tokens))
        self.assertFalse(c._waiters)

    async def test_circuit_stops_fresh_work_and_retries_after_initial_inflight_batch(self):
        c = ProviderCoordinator(AdmissionOptions(failure_threshold=1, tokens_per_request=4), clock=ManualClock())

        class Outage:
            async def acomplete(self, prompt):
                raise RetryableProviderError("outage")

        report = await evaluate_native(Config(), Outage(), options=NativeRuntimeOptions(
            concurrency=2, max_attempts=3, retry_delay_seconds=0), coordinator=c)
        self.assertEqual(report["runtime"]["attempts"], 2)
        self.assertEqual(report["runtime"]["retries"], 0)
        self.assertEqual(report["runtime"]["admission_rejections"], 6)
        self.assertEqual(report["runtime"]["charged_tokens"], 8)
        self.assertTrue(all(r["termination"] == "circuit_open" for r in report["results"]))
        validate_coordinated_report(report)

    async def test_admission_deadline_and_oversized_reservation_are_explicit_rejections(self):
        clock = ManualClock()
        c = ProviderCoordinator(AdmissionOptions(requests_per_minute=1, admission_timeout_seconds=2), clock=clock)
        job = asyncio.create_task(evaluate_native(Config(), AsyncMockProvider(), coordinator=c))
        try:
            await drain()
            for _ in range(5):
                clock.advance(2)
                await drain()
            report = await job
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        self.assertEqual(report["runtime"]["attempts"], 1)
        self.assertEqual(report["runtime"]["admission_rejections"], 5)
        self.assertEqual(report["runtime"]["duration_seconds"], 2)
        validate_coordinated_report(report)
        c = ProviderCoordinator(AdmissionOptions(tokens_per_minute=1), clock=ManualClock())
        report = await evaluate_native(Config(), AsyncMockProvider(), coordinator=c)
        self.assertEqual(report["runtime"]["attempts"], 0)
        self.assertTrue(all(not r["attempts"] for r in report["results"]))
        validate_coordinated_report(report)
        metrics = benchmark_metrics(report, 1)
        self.assertEqual(metrics["attempt_count"], 0)
        self.assertEqual(metrics["retry_count"], 0)
        self.assertIsNone(metrics["attempt_error_rate"])
        self.assertIsNone(metrics["attempt_latency_seconds"])

    async def test_coordinated_report_roundtrip_and_tampering(self):
        c = ProviderCoordinator(AdmissionOptions(tokens_per_request=5))
        original = await evaluate_native(Config(), AsyncMockProvider(), coordinator=c)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            write_json(path, original)
            self.assertEqual(load_report(path), original)
            for field in ("charged_tokens", "attempts", "mean_admission_queue", "admission_rejections"):
                changed = copy.deepcopy(original)
                changed["runtime"][field] += 1
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    load_report(path)


class CoordinationCliTests(unittest.TestCase):
    def test_cli_offline_controls_and_invalid_options_precede_provider_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            config, output = Path(directory) / "config.json", Path(directory) / "report.json"
            write_json(config, Config().to_dict())
            args = ["run", "--config", str(config), "--output", str(output)]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args + ["--native-async", "--requests-per-minute", "100", "--tokens-per-minute",
                                             "10000", "--tokens-per-request", "100"]), 0)
                report = load_report(output)
                self.assertEqual(report["runtime"]["kind"], "native-async-v2")
                self.assertEqual(report["runtime"]["charged_tokens"], 600)
                output.unlink()
                for flags in (["--coordinate"], ["--requests-per-minute", "1"],
                              ["--native-async", "--tokens-per-minute", "0"],
                              ["--native-async", "--retries-per-minute", "-1"],
                              ["--native-async", "--circuit-cooldown", "nan"]):
                    with patch("forge.cli.run_native") as run:
                        self.assertEqual(main(args + flags), 2)
                        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
