# Forge progress

## Native HTTP increment (2026-10-25)

Added optional `http` extra (aiohttp), a separate `AsyncProvider.acomplete()`
protocol, and a context-managed native OpenRouter session. The synchronous
`complete()` API is unchanged. A whole-request deadline covers pool acquisition,
headers, and bounded body reading; cancellation propagates and closes responses.
Transient exceptions carry parsed numeric/HTTP-date Retry-After delays. Redirects
are disabled; credentials and response/error bodies stay out of error messages.
Transport tests use fake responses and events, with no API calls or credentials.
`python -m unittest discover -s tests -q`: 80 passed, 0 failures/errors.
Transport committed as `f4ddb30`; scheduler follow-up completed below.

## Native scheduler increment (2026-10-25)

Added `--native-async` with separate active-request and pending-retry limits.
One central timer heap holds delayed/ready retries; delays release request
capacity. Admission alternates fresh work and due retries when both are ready.
Full jitter uses a task/attempt-specific seed; Retry-After is a minimum delay.
Overflow and excessive server waits terminate explicitly (`retry_queue_full`,
`retry_after_limit`). This is per-request backoff, not a provider-wide rate limiter.
Native runtime evidence is versioned independently; legacy reports still load.

94 tests passed, 0 failures/errors, including a real loopback HTTP 429/retry
exchange and stalled-body deadline with socket disconnect verification. The
sandbox blocks binding loopback sockets; this suite ran with the required
permission. No external services were contacted. Deterministic event/virtual
clock tests cover storms, fairness, cancellation, deadlines, and both bounds.
Scheduler committed as `4cfe3b6`; benchmark follow-up completed below.

## Benchmark increment (2026-10-25)

Benchmark schema 2 adds completed/successful throughput; nearest-rank p50/p95/p99
attempt, task, and completion latency; final task error/failure rates; attempt
error rate; retry counts and runtime peaks. Task latency excludes initial queue
wait; completion latency includes it (unavailable on the sequential baseline).
Synthetic and API modes produce separate tagged artifacts. API mode requires
network opt-in, has no paid warmups or injected failures, and preserves full
reports. No real API measurements were run.

Reproduction (run these separately, not concurrently):

```sh
.venv/bin/python benchmarks/runtime.py --output /tmp/forge-native-synthetic-isolated-20261025.json
.venv/bin/python benchmarks/runtime.py --transient-every 3 --output /tmp/forge-native-retries-isolated-20261025.json
```

Python 3.9.6, macOS 15.0 arm64; 24 tasks, seed 42, three repetitions, 10 ms
simulated wait. Native measurements use an async mock, not HTTP. The following
are medians across repetitions; percentiles are medians of per-run percentiles.

| Clean synthetic runner | Wall s | Completed tasks/s | Task p50 ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sequential | 0.294130 | 81.60 | 12.57 | 12.82 | 12.84 |
| Thread, concurrency 4 | 0.081677 | 293.84 | 12.62 | 13.71 | 13.89 |
| Native, concurrency 4 | 0.075088 | 319.63 | 11.99 | 12.31 | 12.33 |

All 360 clean task outcomes passed; errors/retries were zero. With first-attempt
failure injection every third task, native concurrency 4 had median wall time
0.095321 s, 251.78 completed tasks/s, task p50/p95/p99 11.94/23.86/23.87 ms,
zero final task errors, 25% attempt errors, and eight executed retries per run.
The sequential baseline had 33.33% final task errors and no retries. Concurrent
modes use two attempts versus one for that baseline; these are recovery-policy
observations, not equal-work speedups. No model/GPU performance inference is made.
Raw JSON artifacts stay outside Git. HTTP integration was verified with aiohttp
3.11.11 against a loopback fixture, not a paid API.

Final verification before the benchmark commit: 100 tests passed, 0 failures/errors,
with the HTTP extra installed; `git diff --check` passed. The installed console
entrypoint also passed an offline init → native run → report smoke check (6/6).
Primary protocol and
client references, semantics, and tradeoffs are in `docs/architecture.md`.

## Completed milestones

- `67417cf`: installable offline evaluation CLI, deterministic fixtures, mock and
  gated OpenRouter adapters, JSON reports, and tests.
- `b9c056a`: versioned task suites, trusted reference/mutation validation, suite
  snapshots and digests, and historical report compatibility.
- `66c188f`: seeded, bounded development-suite generation with provenance.
- `ed69fbe`: bounded fixture-backed tool episodes and replay validation.
- `476bc67`: opt-in bounded asynchronous text evaluation, transient failure
  classification, bounded retries, ordered results, attempt/queue/runtime metrics,
  report checks, and an offline sequential/concurrent benchmark. No new runtime
  dependencies. Default sequential and tool paths remain supported.

## Thread compatibility runner decisions (476bc67)

- Use fixed coroutine workers plus a dedicated thread pool to adapt the existing
  blocking provider boundary. Custom providers must be thread-safe. Both submitted
  calls and coroutine count are bounded by concurrency (1-64).
- Retry only explicitly transient errors; default one attempt, maximum five.
  Backoff retains the rollout slot. No jitter, `Retry-After`, or rate limiter yet.
  Retrying remote requests can duplicate billed work.
- Cancellation/error cleanup stops admission and joins running threads. Cleanup
  can block the event loop; provider socket timeouts are not hard run deadlines.
  This compatibility runner has no partial reports, durable restart, or native
  HTTP; the new native path above is separate. Neither path distributes work.
- Public development fixtures remain plumbing checks. No sandboxed coding tasks,
  independent held-out evaluation, trainable dataset, SFT/RL, vLLM, or GPU result.

## Verification and measured experiment (2026-10-25)

Baseline: 60 tests passed. Final `.venv/bin/python -m unittest discover -s tests -v`:
73 tests passed, 0 failures/errors. `git diff --check` passed. Expanded suite covers concurrency overlap and bounds,
out-of-order completion, retry recovery/exhaustion, sanitized failures, wrong and
non-text answers, programming errors, cancellation cleanup, event-loop response,
runtime report consistency, CLI preflight, and network gating.

Command: `.venv/bin/python benchmarks/runtime.py --output /tmp/forge-runtime-benchmark-20261025.json`.
Environment: Python 3.9.6, macOS 15.0, arm64. Seed 42, 24 tasks, 10 ms simulated
blocking I/O per request, three repetitions, rotated run order, unmeasured warmups.
All 216 measured task outcomes passed. Median end-to-end wall times:

| Runner | Concurrency | Seconds |
| --- | ---: | ---: |
| Sequential baseline | 1 | 0.292913 |
| Async thread bridge | 1 | 0.303324 |
| Async thread bridge | 4 | 0.081162 |

The measured baseline/concurrency-4 ratio is 3.61x for this synthetic delay
workload. The concurrency-1 bridge costs overhead here. This does not measure
model inference, GPU utilization, remote throughput, or distributed scaling.
Raw JSON is a generated local artifact and is not committed; the harness records
environment, suite digest, and every sample for reproduction.

## Next recommended task

Add provider-wide rate-limit coordination (fresh-request cooldown/token budget)
with fairness and multi-run tests. Then reuse the native runtime for multiple
tool episodes with trajectory provenance before training or distribution.
Durable restart, a rollout-wide deadline, token accounting, and live API/GPU
measurements remain open. The owner must explicitly authorize any paid run.
