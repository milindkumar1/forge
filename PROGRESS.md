# Forge progress

## Completed milestones

- `67417cf`: installable offline evaluation CLI, deterministic fixtures, mock and
  gated OpenRouter adapters, JSON reports, and tests.
- `b9c056a`: versioned task suites, trusted reference/mutation validation, suite
  snapshots and digests, and historical report compatibility.
- `66c188f`: seeded, bounded development-suite generation with provenance.
- `ed69fbe`: bounded fixture-backed tool episodes and replay validation.
- Current slice: opt-in bounded asynchronous text evaluation, transient failure
  classification, bounded retries, ordered results, attempt/queue/runtime metrics,
  report checks, and an offline sequential/concurrent benchmark. No new runtime
  dependencies. Default sequential and tool paths remain supported.

## Decisions and limitations

- Use fixed coroutine workers plus a dedicated thread pool to adapt the existing
  blocking provider boundary. Custom providers must be thread-safe. Both submitted
  calls and coroutine count are bounded by concurrency (1-64).
- Retry only explicitly transient errors; default one attempt, maximum five.
  Backoff retains the rollout slot. No jitter, `Retry-After`, or rate limiter yet.
  Retrying remote requests can duplicate billed work.
- Cancellation/error cleanup stops admission and joins running threads. Cleanup
  can block the event loop; provider socket timeouts are not hard run deadlines.
  No partial reports, durable restart, native async HTTP, or distributed workers.
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

Add a native asynchronous HTTP provider path with bounded request deadlines,
cancellation, `Retry-After` handling, and offline transport fixtures. Preserve
network opt-in and the sequential baseline. Then reuse the runtime for multiple
tool episodes with trajectory provenance before adding training or distribution.
