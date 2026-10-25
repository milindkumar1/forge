import asyncio
import unittest

from forge.admission import AdmissionOptions, ProviderCoordinator


class ManualClock:
    def __init__(self):
        self.value = 0.0
        self.sleepers = []

    def now(self):
        return self.value

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.value + seconds, future))
        try:
            await future
        finally:
            self.sleepers = [(t, f) for t, f in self.sleepers if f is not future]

    def advance(self, seconds):
        self.value += seconds
        for deadline, future in self.sleepers:
            if deadline <= self.value and not future.done():
                future.set_result(None)


class AdmissionTests(unittest.TestCase):
    def coordinator(self, **fields):
        self.clock = ManualClock()
        return ProviderCoordinator(AdmissionOptions(**fields), clock=self.clock)

    def admit(self, coordinator, tokens=1, retry=False):
        ticket = coordinator.enqueue(tokens, retry=retry)
        decision = coordinator.decide(ticket)
        self.assertIsNotNone(decision.permit)
        return decision.permit

    def test_rpm_burst_and_exact_rolling_boundary(self):
        c = self.coordinator(requests_per_minute=2)
        self.admit(c)
        self.admit(c)
        ticket = c.enqueue(1, retry=False)
        self.assertEqual(c.decide(ticket).delay, 60)
        self.clock.advance(59)
        self.assertEqual(c.decide(ticket).delay, 1)
        self.clock.advance(1)
        self.assertIsNotNone(c.decide(ticket).permit)
        self.assertEqual(c.requests, 3)

    def test_tokens_reserve_reconcile_and_hold_inflight_across_windows(self):
        c = self.coordinator(tokens_per_minute=10)
        permit = self.admit(c, 8)
        ticket = c.enqueue(4, retry=False)
        self.clock.advance(60)
        self.assertIsNone(c.decide(ticket).permit)
        c.complete(permit, actual_tokens=5)
        admitted = c.decide(ticket).permit
        self.assertIsNotNone(admitted)
        self.assertEqual(c.reserved_tokens, 12)
        self.assertEqual(c.charged_tokens, 9)
        c.complete(admitted)  # unknown usage retains all four reserved tokens
        self.clock.advance(59)
        waiting = c.enqueue(2, retry=False)
        self.assertEqual(c.decide(waiting).delay, 1)
        self.clock.advance(1)
        self.assertIsNotNone(c.decide(waiting).permit)

    def test_usage_overrun_becomes_debt_instead_of_silent_clamping(self):
        c = self.coordinator(tokens_per_minute=10)
        permit = self.admit(c, 5)
        c.complete(permit, actual_tokens=13)
        ticket = c.enqueue(1, retry=False)
        self.assertEqual(c.decide(ticket).delay, 60)
        self.assertEqual(c.charged_tokens, 13)

    def test_retries_charge_all_quotas_and_budget_is_bounded(self):
        c = self.coordinator(requests_per_minute=3, tokens_per_minute=30, retries_per_minute=1)
        c.complete(self.admit(c, 5), failed=True)
        c.complete(self.admit(c, 5, retry=True), failed=True)
        ticket = c.enqueue(5, retry=True)
        self.assertEqual(c.decide(ticket).rejection, "retry_budget_exhausted")
        self.assertEqual((c.requests, c.retries, c.charged_tokens), (2, 1, 10))
        self.clock.advance(60)
        self.admit(c, 5, retry=True)
        self.assertEqual(c.retries, 2)

    def test_fifo_prevents_small_requests_starving_large_request_and_cancel_unblocks(self):
        c = self.coordinator(tokens_per_minute=10)
        charged = self.admit(c, 4)
        large, small = c.enqueue(8, retry=False), c.enqueue(1, retry=False)
        self.assertIsNone(c.decide(large).permit)
        self.assertIsNone(c.decide(small).permit)
        c.complete(charged, actual_tokens=1)
        permit = c.decide(large).permit
        self.assertIsNotNone(permit)
        self.assertIsNotNone(c.decide(small).permit)
        extra = c.enqueue(1, retry=False)
        c.cancel(extra)
        with self.assertRaises(ValueError):
            c.decide(extra)

    def test_oversized_and_expired_tickets_do_not_consume_requests(self):
        c = self.coordinator(tokens_per_minute=5, admission_timeout_seconds=2)
        oversized = c.enqueue(6, retry=False)
        self.assertEqual(c.decide(oversized).rejection, "token_budget_exceeded")
        waiting = c.enqueue(3, retry=False)
        self.clock.advance(2)
        self.assertEqual(c.decide(waiting).rejection, "admission_timeout")
        self.assertEqual(c.requests, 0)

    def test_global_cooldown_extends_never_shortens_and_uses_monotonic_clock(self):
        c = self.coordinator()
        first, second = self.admit(c), self.admit(c)
        c.complete(first, failed=True, cooldown_seconds=12)
        self.clock.advance(2)
        c.complete(second, failed=True, cooldown_seconds=1)
        ticket = c.enqueue(1, retry=False)
        self.assertEqual(c.decide(ticket).delay, 10)
        self.clock.advance(10)
        self.assertIsNotNone(c.decide(ticket).permit)

    def test_breaker_rejects_then_allows_one_probe_and_ignores_stale_success(self):
        c = self.coordinator(failure_threshold=1, circuit_cooldown_seconds=10)
        failed, stale = self.admit(c), self.admit(c)
        c.complete(failed, failed=True)
        c.complete(stale)  # success already in flight cannot undo an open circuit
        self.assertEqual(c.circuit, "open")
        self.assertEqual(c.decide(c.enqueue(1, retry=False)).rejection, "circuit_open")
        self.clock.advance(10)
        probe = self.admit(c)
        self.assertTrue(probe.probe)
        self.assertEqual(c.decide(c.enqueue(1, retry=False)).rejection, "circuit_open")
        c.complete(probe)
        self.assertEqual(c.circuit, "closed")
        c.complete(self.admit(c), failed=True)
        self.clock.advance(10)
        c.complete(self.admit(c), failed=True)
        self.assertEqual(c.circuit, "open")
        self.assertEqual(c.circuit_opens, 3)

    def test_cancelled_probe_releases_half_open_state_and_keeps_unknown_charge(self):
        c = self.coordinator(failure_threshold=1, circuit_cooldown_seconds=1)
        c.complete(self.admit(c, 3), failed=True)
        self.clock.advance(1)
        probe = self.admit(c, 3)
        c.complete(probe, cancelled=True)
        self.assertEqual(c.circuit, "open")
        self.assertEqual(c.charged_tokens, 6)

    def test_queue_bound_and_invalid_settings(self):
        c = self.coordinator(max_waiters=1)
        c.enqueue(1, retry=False)
        self.assertIsNone(c.enqueue(1, retry=False))
        for fields in ({"requests_per_minute": 0}, {"tokens_per_minute": True}, {"tokens_per_request": -1},
                       {"retries_per_minute": -1}, {"failure_threshold": True}, {"max_waiters": 0},
                       {"circuit_cooldown_seconds": float("nan")}, {"admission_timeout_seconds": 0}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                AdmissionOptions(**fields)


class AdmissionWakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_feedback_wakes_waiter_even_before_coroutine_starts(self):
        c = ProviderCoordinator()
        waiter = c.wait(None)
        c.cancel(c.enqueue(1, retry=False))
        await asyncio.wait_for(waiter, 1)

    async def test_cancellation_removes_clock_sleepers(self):
        clock = ManualClock()
        c = ProviderCoordinator(clock=clock)
        waiter = asyncio.create_task(c.wait(60))
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(len(clock.sleepers), 1)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(clock.sleepers, [])


if __name__ == "__main__":
    unittest.main()
