"""Event-loop-local provider quotas, FIFO admission, retry budget and breaker."""

import asyncio
import math
from collections import deque
from dataclasses import dataclass
from time import monotonic
from typing import Awaitable, Deque, List, Optional, Protocol


class Clock(Protocol):
    def now(self) -> float:
        ...

    async def sleep(self, seconds: float) -> None:
        ...


class MonotonicClock:
    def now(self) -> float:
        return monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True)
class AdmissionOptions:
    requests_per_minute: Optional[int] = None
    tokens_per_minute: Optional[int] = None
    tokens_per_request: Optional[int] = None
    retries_per_minute: int = 64
    failure_threshold: int = 5
    circuit_cooldown_seconds: float = 30.0
    admission_timeout_seconds: float = 300.0
    max_waiters: int = 64

    def __post_init__(self) -> None:
        for name in ("requests_per_minute", "tokens_per_minute", "tokens_per_request"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 1 <= value <= 1_000_000_000):
                raise ValueError(name + " must be an integer in [1, 1000000000] or null")
        for name, low, high in (("retries_per_minute", 0, 1000000), ("failure_threshold", 0, 1000000),
                                ("max_waiters", 1, 1000)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(name + " is outside its integer bounds")
        for name in ("circuit_cooldown_seconds", "admission_timeout_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 3600:
                raise ValueError(name + " must be finite in (0, 3600]")

    def reservation(self, prompt: str) -> int:
        # A documented estimate, not a tokenizer: bytes + chat allowance + output cap.
        return self.tokens_per_request or len(prompt.encode("utf-8")) + 64 + 256


@dataclass(eq=False)
class Ticket:
    tokens: int
    retry: bool
    queued_at: float


@dataclass(eq=False)
class Permit:
    tokens: int
    epoch: int
    probe: bool
    expires_at: Optional[float] = None
    settled: bool = False


@dataclass(frozen=True)
class Decision:
    permit: Optional[Permit] = None
    delay: Optional[float] = None
    rejection: Optional[str] = None


class ProviderCoordinator:
    """Share explicitly for one quota scope on one asyncio event loop.

    All mutation is synchronous/atomic with respect to that event loop. FIFO
    tickets prevent cheap requests from starving a larger token reservation.
    No active request slot is acquired here. Do not share across threads/loops.
    """

    def __init__(self, options: AdmissionOptions = AdmissionOptions(), *, clock: Optional[Clock] = None) -> None:
        self.options = options
        self.clock = clock or MonotonicClock()
        self._waiters: Deque[Ticket] = deque()
        self._requests: Deque[float] = deque()
        self._retries: Deque[float] = deque()
        self._tokens: List[Permit] = []
        self._changed: Optional[asyncio.Event] = None
        self.cooldown_until = 0.0
        self.circuit = "closed"
        self.open_until = 0.0
        self.failures = self.epoch = 0
        self.requests = self.retries = self.reserved_tokens = self.charged_tokens = 0
        self.peak_waiters = self.circuit_opens = 0

    def _notify(self) -> None:
        if self._changed is not None:
            self._changed.set()
        self._changed = None

    def enqueue(self, tokens: int, *, retry: bool, ready_at: Optional[float] = None) -> Optional[Ticket]:
        if type(tokens) is not int or tokens < 1 or type(retry) is not bool:
            raise ValueError("invalid admission reservation")
        if len(self._waiters) >= self.options.max_waiters:
            return None
        now = self.clock.now()
        if ready_at is not None and (type(ready_at) not in (int, float) or not math.isfinite(ready_at) or ready_at > now):
            raise ValueError("invalid admission readiness timestamp")
        ticket = Ticket(tokens, retry, now if ready_at is None else ready_at)
        self._waiters.append(ticket)
        self.peak_waiters = max(self.peak_waiters, len(self._waiters))
        self._notify()
        return ticket

    def cancel(self, ticket: Ticket) -> None:
        if ticket in self._waiters:
            self._waiters.remove(ticket)
            self._notify()

    def _prune(self, now: float) -> None:
        while self._requests and self._requests[0] <= now - 60:
            self._requests.popleft()
        while self._retries and self._retries[0] <= now - 60:
            self._retries.popleft()
        self._tokens = [charge for charge in self._tokens if charge.tokens and
                        (charge.expires_at is None or charge.expires_at > now)]

    def decide(self, ticket: Ticket) -> Decision:
        if ticket not in self._waiters:
            raise ValueError("ticket is no longer pending")
        now = self.clock.now()
        self._prune(now)
        rejection = None
        remaining = ticket.queued_at + self.options.admission_timeout_seconds - now
        if remaining <= 0:
            rejection = "admission_timeout"
        elif self.options.tokens_per_minute is not None and ticket.tokens > self.options.tokens_per_minute:
            rejection = "token_budget_exceeded"
        elif self.circuit == "half_open" or (self.circuit == "open" and now < self.open_until):
            rejection = "circuit_open"
        elif ticket.retry and len(self._retries) >= self.options.retries_per_minute:
            rejection = "retry_budget_exhausted"
        if rejection is not None:
            self.cancel(ticket)
            return Decision(rejection=rejection)
        if self._waiters[0] is not ticket:
            return Decision(delay=remaining)
        delay = max(0.0, self.cooldown_until - now)
        if self.options.requests_per_minute is not None and len(self._requests) >= self.options.requests_per_minute:
            delay = max(delay, self._requests[0] + 60 - now)
        if self.options.tokens_per_minute is not None:
            excess = sum(charge.tokens for charge in self._tokens) + ticket.tokens - self.options.tokens_per_minute
            if excess > 0:
                # In-flight charges cannot expire before settlement. A completion
                # wakes the waiter; completed charges expire 60 seconds later.
                for charge in sorted((c for c in self._tokens if c.expires_at is not None),
                                     key=lambda c: c.expires_at):
                    excess -= charge.tokens
                    if excess <= 0:
                        delay = max(delay, charge.expires_at - now)
                        break
                if excess > 0:
                    return Decision(delay=remaining)
        if delay > 0:
            return Decision(delay=min(delay, remaining))
        self._waiters.popleft()
        probe = self.circuit == "open"
        if probe:
            self.circuit = "half_open"
        permit = Permit(ticket.tokens, self.epoch, probe)
        if self.options.requests_per_minute is not None:
            self._requests.append(now)
        if self.options.tokens_per_minute is not None:
            self._tokens.append(permit)
        if ticket.retry:
            self._retries.append(now)
            self.retries += 1
        self.requests += 1
        self.reserved_tokens += ticket.tokens
        self.charged_tokens += ticket.tokens
        self._notify()
        return Decision(permit=permit)

    def complete(self, permit: Permit, *, actual_tokens: Optional[int] = None, failed: bool = False,
                 cooldown_seconds: float = 0.0, cancelled: bool = False) -> None:
        if permit.settled:
            raise ValueError("permit already settled")
        if actual_tokens is not None and (type(actual_tokens) is not int or actual_tokens < 0):
            raise ValueError("invalid provider token usage")
        if (type(cooldown_seconds) not in (int, float) or not math.isfinite(cooldown_seconds)
                or cooldown_seconds < 0):
            raise ValueError("invalid provider cooldown")
        now = self.clock.now()
        permit.settled = True
        permit.expires_at = now + 60
        if actual_tokens is not None:
            self.charged_tokens += actual_tokens - permit.tokens
            permit.tokens = actual_tokens
        self.cooldown_until = max(self.cooldown_until, now + cooldown_seconds)
        if permit.epoch == self.epoch and self.options.failure_threshold:
            if cancelled and permit.probe:
                self._open(now)
            elif not cancelled:
                if failed:
                    self.failures += 1
                    if permit.probe or self.failures >= self.options.failure_threshold:
                        self._open(now)
                elif self.circuit != "open":
                    self.failures = 0
                    self.circuit = "closed"
        self._notify()

    def _open(self, now: float) -> None:
        self.epoch += 1
        self.circuit = "open"
        self.open_until = now + self.options.circuit_cooldown_seconds
        self.circuit_opens += 1

    def wait(self, delay: Optional[float]) -> Awaitable[None]:
        """Wake on feedback/cancellation or a monotonic timer; no lost wakeups.

        Call immediately after decide(), without an intervening await.
        """
        if self._changed is None:
            self._changed = asyncio.Event()
        return self._wait(self._changed, delay)

    async def _wait(self, changed: asyncio.Event, delay: Optional[float]) -> None:
        if delay is None:
            await changed.wait()
            return
        tasks = [asyncio.create_task(changed.wait()), asyncio.create_task(self.clock.sleep(delay))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
