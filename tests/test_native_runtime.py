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

from forge.cli import main
from forge.config import Config, write_json
from forge.evaluation import evaluate, load_report
from forge.native_runtime import NativeRuntimeOptions, evaluate_native, retry_delay, validate_native_report
from forge.providers import AsyncMockProvider, MockProvider, ProviderError, RetryableProviderError
from forge.suites import DEFAULT_SUITE


class Clock:
    """Advance retry timers deterministically without real backoff sleeps."""
    def __init__(self):
        self.now = 1.0
        self.sleeps = []

    def __call__(self):
        self.now += 0.000001
        return self.now

    async def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


class NativeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_equivalence_and_active_bound_with_controlled_completion(self):
        entered = asyncio.Queue()
        releases = [asyncio.Event() for _ in DEFAULT_SUITE.tasks]
        prompts = [task.prompt for task in DEFAULT_SUITE.tasks]
        active = peak = 0

        class Controlled(AsyncMockProvider):
            async def acomplete(self, prompt):
                nonlocal active, peak
                index = prompts.index(prompt)
                active += 1
                peak = max(peak, active)
                entered.put_nowait(index)
                try:
                    await releases[index].wait()
                    return await super().acomplete(prompt)
                finally:
                    active -= 1

        job = asyncio.create_task(evaluate_native(Config(), Controlled(), options=NativeRuntimeOptions(2)))
        try:
            self.assertEqual([await entered.get(), await entered.get()], [0, 1])
            self.assertTrue(entered.empty())
            releases[1].set()
            self.assertEqual(await entered.get(), 2)
            for release in releases:
                release.set()
            report = await job
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)
        self.assertEqual(report["summary"], evaluate(Config(), MockProvider())["summary"])
        self.assertEqual([r["task_id"] for r in report["results"]], [t.task_id for t in DEFAULT_SUITE.tasks])
        validate_native_report(report)

    async def test_retry_storm_has_independent_pending_limit_and_explicit_shedding(self):
        class Outage:
            async def acomplete(self, prompt):
                raise RetryableProviderError("secret", retry_after_seconds=60)

        clock = Clock()
        with patch("forge.native_runtime.perf_counter", clock), patch("forge.native_runtime.asyncio.sleep", clock.sleep):
            report = await evaluate_native(Config(), Outage(), options=NativeRuntimeOptions(
                concurrency=3, max_attempts=2, max_pending_retries=2))
        self.assertEqual(report["runtime"]["peak_in_flight"], 3)
        self.assertEqual(report["runtime"]["peak_pending_retries"], 2)
        self.assertEqual(report["runtime"]["retry_rejections"], 4)
        self.assertEqual(report["runtime"]["retries"], 2)
        self.assertEqual(report["summary"]["errors"], 6)
        self.assertNotIn("secret", json.dumps(report))
        self.assertTrue(clock.sleeps)
        validate_native_report(report)

    async def test_fresh_and_due_retries_alternate_without_starvation(self):
        starts = []
        counts = {}
        prompts = [task.prompt for task in DEFAULT_SUITE.tasks]

        class Flaky(AsyncMockProvider):
            async def acomplete(self, prompt):
                index = prompts.index(prompt)
                counts[index] = counts.get(index, 0) + 1
                starts.append((index, counts[index]))
                if counts[index] < 3:
                    raise RetryableProviderError("transient")
                return await super().acomplete(prompt)

        report = await evaluate_native(Config(), Flaky(), options=NativeRuntimeOptions(
            concurrency=1, max_attempts=3, retry_delay_seconds=0))
        self.assertEqual(starts[:8], [(0, 1), (0, 2), (1, 1), (0, 3), (2, 1), (1, 2), (3, 1), (2, 2)])
        self.assertEqual(report["summary"]["passed"], 6)
        self.assertEqual(report["runtime"]["retries"], 12)

    async def test_delayed_retry_releases_request_slot_and_honors_floor(self):
        starts = []
        clock = Clock()

        class Flaky(AsyncMockProvider):
            async def acomplete(self, prompt):
                starts.append((prompt, clock.now))
                if len(starts) == 1:
                    raise RetryableProviderError("transient", retry_after_seconds=7)
                return await super().acomplete(prompt)

        with patch("forge.native_runtime.perf_counter", clock), patch("forge.native_runtime.asyncio.sleep", clock.sleep):
            report = await evaluate_native(Config(), Flaky(), options=NativeRuntimeOptions(
                concurrency=1, max_attempts=2, max_pending_retries=1))
        self.assertEqual([p for p, _ in starts[:6]], [t.prompt for t in DEFAULT_SUITE.tasks])
        self.assertEqual(starts[-1][0], DEFAULT_SUITE.tasks[0].prompt)
        self.assertGreaterEqual(starts[-1][1] - starts[0][1], 7)
        self.assertEqual(report["results"][0]["attempts"][0]["retry_delay_seconds"], 7)
        validate_native_report(report)

    async def test_timeout_cancels_provider_before_releasing_capacity(self):
        active = peak = closed = 0

        class NeverReturns:
            async def acomplete(self, prompt):
                nonlocal active, peak, closed
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.Event().wait()
                finally:
                    active -= 1
                    closed += 1

        report = await evaluate_native(Config(), NeverReturns(), options=NativeRuntimeOptions(
            concurrency=2, max_attempts=2, retry_delay_seconds=0, request_timeout_seconds=0.005))
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)
        self.assertEqual(closed, 12)
        self.assertTrue(all(a["timed_out"] for r in report["results"] for a in r["attempts"]))
        self.assertEqual(report["runtime"]["retries"], 6)
        validate_native_report(report)

    async def test_due_retry_admits_while_another_request_is_still_active(self):
        retry_started = asyncio.Event()
        counts = {}
        first = DEFAULT_SUITE.tasks[0].prompt
        second = DEFAULT_SUITE.tasks[1].prompt

        class Mixed(AsyncMockProvider):
            async def acomplete(self, prompt):
                counts[prompt] = counts.get(prompt, 0) + 1
                if prompt == first:
                    if counts[prompt] == 1:
                        raise RetryableProviderError("transient", retry_after_seconds=0.001)
                    retry_started.set()
                if prompt == second:
                    await retry_started.wait()
                return await super().acomplete(prompt)

        # A liveness guard, not a performance threshold; the long request can
        # finish only after the scheduler's retry timer wakes its idle slot.
        report = await asyncio.wait_for(evaluate_native(Config(), Mixed(), options=NativeRuntimeOptions(
            concurrency=2, max_attempts=2, retry_delay_seconds=0)), 3)
        self.assertEqual(report["summary"]["passed"], 6)
        self.assertEqual(report["runtime"]["retries"], 1)

    async def test_cancellation_cleans_active_requests_without_admitting_more(self):
        entered = asyncio.Queue()
        closed = []

        class Blocked:
            async def acomplete(self, prompt):
                entered.put_nowait(prompt)
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.append(prompt)

        job = asyncio.create_task(evaluate_native(Config(), Blocked(), options=NativeRuntimeOptions(2)))
        await entered.get()
        await entered.get()
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertEqual(len(closed), 2)
        self.assertTrue(entered.empty())

    async def test_cancellation_discards_pending_retry_timer(self):
        waiting = asyncio.Event()

        async def blocked_sleep(delay):
            waiting.set()
            await asyncio.Event().wait()

        class Outage:
            async def acomplete(self, prompt):
                raise RetryableProviderError("outage", retry_after_seconds=60)

        with patch("forge.native_runtime.asyncio.sleep", blocked_sleep):
            job = asyncio.create_task(evaluate_native(Config(), Outage(), options=NativeRuntimeOptions(
                concurrency=2, max_attempts=2)))
            await waiting.wait()
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job

    async def test_permanent_errors_wrong_answers_nontext_and_retry_limits(self):
        class Broken:
            def __init__(self, outcome):
                self.outcome = outcome

            async def acomplete(self, prompt):
                if isinstance(self.outcome, Exception):
                    raise self.outcome
                return self.outcome

        cases = [(ProviderError("secret"), {}, "provider_error"), ("wrong", {}, "answered"),
                 (None, {}, "provider_error"),
                 (RetryableProviderError("secret"), {"max_pending_retries": 0}, "retry_queue_full"),
                 (RetryableProviderError("secret", retry_after_seconds=301), {}, "retry_after_limit")]
        for outcome, fields, termination in cases:
            with self.subTest(termination=termination):
                report = await evaluate_native(Config(), Broken(outcome), options=NativeRuntimeOptions(
                    max_attempts=2, **fields))
                self.assertEqual(report["runtime"]["attempts"], 6)
                self.assertTrue(all(r["termination"] == termination for r in report["results"]))
                validate_native_report(report)

    async def test_programming_errors_cancel_siblings_and_propagate(self):
        both = asyncio.Event()
        closed = []
        count = 0

        class Buggy:
            async def acomplete(self, prompt):
                nonlocal count
                count += 1
                if count == 2:
                    both.set()
                try:
                    await both.wait()
                    if prompt == DEFAULT_SUITE.tasks[0].prompt:
                        raise RuntimeError("bug")
                    await asyncio.Event().wait()
                finally:
                    closed.append(prompt)

        with self.assertRaisesRegex(RuntimeError, "bug"):
            await evaluate_native(Config(), Buggy(), options=NativeRuntimeOptions(2))
        self.assertEqual(len(closed), 2)

    async def test_invalid_suite_prevents_provider_calls(self):
        provider = unittest.mock.Mock()
        suite = replace(DEFAULT_SUITE, tasks=(replace(DEFAULT_SUITE.tasks[0], expected="bad"),))
        with self.assertRaises(ValueError):
            await evaluate_native(Config(), provider, suite)
        provider.acomplete.assert_not_called()


class NativeContractTests(unittest.TestCase):
    def test_options_and_deterministic_jitter(self):
        for fields in ({"max_pending_retries": -1}, {"max_pending_retries": True},
                       {"max_pending_retries": 1001}, {"request_timeout_seconds": 0},
                       {"request_timeout_seconds": float("inf")}, {"retry_seed": -1},
                       {"retry_seed": True}, {"max_retry_after_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                NativeRuntimeOptions(**fields)
        options = NativeRuntimeOptions(retry_delay_seconds=2)
        values = [retry_delay(options, index, 2, 0) for index in range(20)]
        self.assertTrue(all(0 <= value <= 4 for value in values))
        self.assertGreater(len(set(values)), 15)
        self.assertEqual(values, [retry_delay(options, i, 2, 0) for i in range(20)])
        self.assertNotEqual(values, [retry_delay(replace(options, retry_seed=1), i, 2, 0) for i in range(20)])
        self.assertEqual(retry_delay(options, 0, 2, 7), 7)

    def test_report_roundtrip_and_corrupt_evidence_rejection(self):
        original = asyncio.run(evaluate_native(Config(), AsyncMockProvider()))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            write_json(path, original)
            self.assertEqual(load_report(path), original)
            for field, value in (("retries", 1), ("peak_pending_retries", 1001), ("peak_in_flight", 65),
                                 ("retry_rejections", 1), ("duration_seconds", float("nan"))):
                report = copy.deepcopy(original)
                report["runtime"][field] = value
                path.write_text(json.dumps(report))
                with self.assertRaises(ValueError):
                    load_report(path)
            report = copy.deepcopy(original)
            report["results"][0]["termination"] = "retry_queue_full"
            path.write_text(json.dumps(report))
            with self.assertRaises(ValueError):
                load_report(path)

    def test_native_cli_offline_and_option_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            output = Path(directory) / "report.json"
            write_json(config, Config().to_dict())
            args = ["run", "--config", str(config), "--output", str(output)]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args + ["--native-async", "--concurrency", "3"]), 0)
                self.assertEqual(load_report(output)["runtime"]["kind"], "native-async-v1")
                with patch("forge.cli.run_native") as run:
                    self.assertEqual(main(args + ["--native-async"]), 2)
                    run.assert_not_called()
                output.unlink()
                for flags in (["--request-timeout", "3"], ["--native-async", "--request-timeout", "0"],
                              ["--native-async", "--max-pending-retries", "-1"]):
                    with patch("forge.cli.run_native") as run:
                        self.assertEqual(main(args + flags), 2)
                        run.assert_not_called()
                        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
