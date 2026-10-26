# Provider admission

Coordination is opt-in through `--coordinate` or any quota/failure flag with
`--native-async`. `admission.py` owns provider-scoped state; `native_runtime.py`
asks permission before allocating an active request task. One scheduler holds at
most one coordinator ticket. Local fresh/retry alternation is preserved; shared
FIFO tickets prevent a run or cheap token reservation bypassing older work.
FIFO trades some utilization for starvation prevention. Admission expiry provides
a bound when work cannot obtain capacity.

## Configuration

| CLI setting | Coordination default | Meaning |
| --- | --- | --- |
| `--requests-per-minute` | unlimited | Rolling 60-second dispatch budget |
| `--tokens-per-minute` | unlimited | Reserved/reconciled token budget |
| `--tokens-per-request` | prompt UTF-8 bytes + 64 + 256 | Per-attempt reservation estimate |
| `--retries-per-minute` | 64 | Rolling shared retry budget; exhaustion rejects |
| `--failure-threshold` | 5 | Consecutive completed provider errors before opening; 0 disables |
| `--circuit-cooldown` | 30 seconds | Open interval before a single recovery probe |
| `--admission-timeout` | 300 seconds | Maximum wait since a task/attempt became eligible |
| `--max-admission-waiters` | 64 | Shared FIFO ticket capacity |

RPM, TPM, and reservations accept integers in [1, 1000000000]. Retry budget and
failure threshold accept [0, 1000000]; waiter capacity accepts [1, 1000]. Time
settings must be finite in (0, 3600]. Existing concurrency, pending-retry, attempt,
request deadline, jitter, and excessive-server-wait controls still apply.

## Quotas and token accounting

Request charges expire exactly 60 seconds after dispatch. Initial bursts up to
the full quota are allowed; this is a rolling window, not evenly spaced pacing.
Every admitted retry consumes RPM/TPM plus shared retry budget again. Cancelled
dispatched requests retain charges even if transport delivery is uncertain.
Rejections before dispatch consume no request, token, or retry quota.

TPM reserves before dispatch. In-flight reservations do not expire; successful
optional `Completion.total_tokens` metadata reconciles the charge. Native HTTP
parses `usage.total_tokens` while keeping `acomplete() -> str` unchanged. Unknown
usage, including timeout/error/cancellation, retains the reservation. Settled
charges expire 60 seconds after completion, conservatively bounding slow calls.
Zero usage can release reserved token capacity immediately. Actual usage above
the reservation becomes debt blocking later admission rather than being clamped.

The default reservation is **an estimate, not a tokenizer or a proven upper
bound**. Exact admission guarantees require a model-specific tokenizer and output
allowance. An underestimate cannot undo already-consumed provider tokens. Supply
`tokens_per_request` appropriately until tokenizer integration exists. Requests
whose reservation exceeds the entire TPM budget fail explicitly. These local
limits need not match a provider's undocumented or differently scoped policy.

The [OpenRouter response schema](https://openrouter.ai/docs/api_reference/overview)
documents usage metadata. It is provider evidence, not a locally measured token
count. Reports preserve reported versus reserved/charged values separately.

## Cooldown, retry budget, and circuit breaker

HTTP 429 pauses all new admissions in this scope. Retry-After is a minimum wait;
without a usable header, the native jitter delay supplies the cooldown. Other
transient Retry-After feedback also extends the shared cooldown. Already active
requests continue. HTTP-date parsing converts wall time into a delay once; all
subsequent scheduling uses monotonic time. New feedback can extend but cannot
shorten the cooldown. Admission expiry prevents waiting indefinitely for very
large server minima; the scheduler never retries early to satisfy a local cap.
Protocol semantics follow [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after).

The retry budget bounds dispatched retries in each rolling minute, independently
of pending retry capacity and per-task attempts. Exhaustion rejects immediately
rather than queueing another retry until replenishment. First attempts do not
spend this retry budget but do consume RPM/TPM.

The circuit opens after the configured number of consecutive completed provider
errors, including timeouts, 429s, and permanent provider errors. A correct HTTP
response with a wrong model answer counts as transport success. Open circuits
reject admissions. After cooldown one half-open probe may run; success closes,
failure reopens. Generation numbers prevent stale successes from closing a newer
open circuit. Cancelling a probe reopens and releases half-open state. In-flight
requests at opening may finish; the threshold is not a hard total-call cap.

## Scope, cancellation, and evidence

Share a `ProviderCoordinator(AdmissionOptions(...))` via `evaluate_native(...,
coordinator=coordinator)` across workers/runs for **one account/model quota scope
on one event loop**. State changes are synchronous within the loop; no thread or
process safety is claimed. The CLI owns one coordinator per invocation. A
multi-process quota service is not implemented.

FIFO tickets, an event broadcast, and one timer per scheduler wait wake blocked
admission on settlement or expiry. Waiting never occupies a request slot.
Cancellation removes tickets/timers and settles unknown in-flight usage without
refund. Async providers must cooperate with cancellation. Readiness-based
admission deadlines include request-slot queueing; retry backoff before readiness
is excluded. An injected `Clock` supplies monotonic `now()` and `sleep()`; request
I/O deadlines continue to use asyncio's real event-loop timer.

Coordinated runtime reports use `native-async-v2`; prior native/thread and
synchronous report formats remain supported. Unsent work has zero attempts and
an explicit termination (`token_budget_exceeded`, `admission_timeout`,
`admission_queue_full`, `retry_budget_exhausted`, or `circuit_open`). Admission
errors count in the existing summary's error total. Per-run counts derive from
its attempts; scope counters are cumulative snapshots when shared across runs.

Per-attempt admission latency starts when eligible and ends when permission is
obtained. Per-task admission latency sums these waits, including a rejected final
admission. Mean ready-queue depth is the sum of these waits divided by run time,
equivalently the time-integral of ready-but-unadmitted backlog. Peak includes all
initially ready suite tasks. Retry backoff is excluded until its timer is due.
Native v1 adds these timing metrics without changing its existing attempt schema.

## Synthetic limit experiment

`benchmarks/runtime.py --mode limits` compares uncoordinated/coordinated native
scheduling with equal concurrency and three attempts per task. Defaults are 24
seeded tasks, three repetitions, 4 RPM, 12 TPM, and three exact synthetic token
units per accepted request. The server consumes quota only on accepted requests;
clients conservatively charge all dispatched attempts. Service is instantaneous.
Transient mode fails each task's first accepted attempt; outage mode fails every
accepted attempt. The client admission deadline is 3600 simulated seconds.

`--simulated-rpm`, `--simulated-tpm`, `--simulated-tokens`, `--simulation-scale`,
`--retry-budget`, and `--breaker-threshold` control the experiment. A monotonic
clock scaled by 600 makes a simulated minute last 0.1 real seconds. The artifact
records scale and both time bases; quota logic still uses rolling 60-second
windows. Request I/O timeout behavior is tested separately, not modeled here.

Artifacts remain tagged `synthetic` with `metrics_timebase: simulated_seconds`.
They include raw reports, admission percentiles, queue depth, admission and
provider rejections, retry counts, and completed/successful throughput. The
uncoordinated baseline has no client admission rejections (null), but can receive
simulated server 429s. All-rejected runs have no attempt latency/error rate (null),
not an invented zero. Rapid failures can inflate completed-task throughput; run
horizons and burst allowances differ, so compare success and costs alongside
throughput. These numbers do not measure model, GPU, or HTTP service performance.
