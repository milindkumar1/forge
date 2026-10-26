# Forge progress

## Current state (2026-10-25)

Working offline evaluation CLI, validated/versioned development tasks, seeded
generation, bounded tool fixture, synchronous and native asynchronous providers,
fair concurrent scheduling, retries, request deadlines, and shared provider
admission controls. Synchronous/thread/native v1 behavior and historical reports
remain supported; coordination is opt-in. No paid API calls, model/GPU benchmark,
training, or distributed execution was performed or claimed.

## Latest increments

- `f73abec`: shared provider coordinator and optional HTTP token usage metadata;
  113 tests passed. Rolling RPM/TPM, FIFO tickets, monotonic provider cooldown,
  shared retry budget, and closed/open/single-probe half-open breaker.
- `c524001`: coordinated native admission, CLI controls, native runtime v2 evidence,
  and shared-run/virtual-clock tests; 125 tests passed. Admission waits do not
  occupy active request slots; retries reacquire all applicable budgets.
- `f374a8e`: preserve readiness-based admission deadlines and count quota-blocked
  retries against pending capacity; 130 tests passed.
- Current benchmark increment: admission p50/p95/p99, mean/peak ready backlog,
  admission/provider rejection counts, and completed/successful throughput under
  accelerated monotonic simulated provider limits. Readiness-based admission
  deadlines include time spent waiting for active request slots.

Final suite: `.venv/bin/python -m unittest discover -s tests -q` — 130 tests
passed, 0 failures/errors with the HTTP extra installed. Loopback HTTP integration
requires permission to bind a temporary local socket in restricted sandboxes;
all other provider fixtures stay offline. All 18 embedded benchmark reports
passed report-reader validation. `git diff --check` passed.
The installed console entrypoint passed an offline coordinated init → run →
report smoke check (6/6; all six reservations charged, no network calls).

## Technical decisions and open limits

- A coordinator is explicitly shared for one account/model quota scope on one
  event loop. The CLI owns one per invocation. No cross-process quota service.
- RPM counts dispatches in rolling 60-second windows, including retries and
  uncertain/cancelled requests. Initial bursts are allowed. No request slot is
  consumed before permission. FIFO admission avoids starvation by cheaper work,
  but can leave capacity idle behind a large token reservation.
- TPM reserves before dispatch; optional `usage.total_tokens` reconciles the
  charge. Unknown usage retains the reservation. In-flight reservations do not
  expire; settled charges expire 60 seconds after completion. Underestimates
  become debt. Default bytes + chat/output allowance is an estimate, not exact
  tokenization or a guaranteed upper bound. Model-specific tokenization is open.
- Global cooldown honors Retry-After without shortening server minima. Shared
  retries per rolling minute, per-task attempts, and pending-retry capacity bound
  storms. Circuit thresholds count completed provider errors; already active
  requests may finish after opening. Probe cancellation reopens safely.
- Native runtime v2 records unsent rejections with zero attempts, admission waits,
  reservation/reported/charged tokens, and cumulative scope snapshots. Per-run
  counters come from local attempts. Async cancellation remains cooperative.
- See [provider admission](docs/provider-admission.md) for policy, configuration,
  primary references, and benchmark semantics. Durable restart and multi-episode
  trajectories remain open; public fixtures are not held-out capability evidence.

## Synthetic provider-limit observations

Reproduce separately (new output paths required):

```sh
.venv/bin/python benchmarks/runtime.py --mode limits --output /tmp/forge-limits-clean.json
.venv/bin/python benchmarks/runtime.py --mode limits --scenario transient --output /tmp/forge-limits-transient.json
.venv/bin/python benchmarks/runtime.py --mode limits --scenario outage --output /tmp/forge-limits-outage.json
```

Python 3.9.6, macOS 15 arm64. Each experiment uses 24 seeded tasks, three
repetitions, concurrency 4, three attempts/task, 4 RPM/12 TPM, and three exact
synthetic tokens per accepted request. Time scale 600; service time is zero.
Client retry budget is four per simulated minute; breaker threshold is five;
admission deadline is 3600 simulated seconds. The simulated server charges only
accepted requests; the coordinated client charges all dispatches conservatively.
No real model, GPU, or HTTP-serving performance is measured.

Clean traffic results (medians; time/throughput use simulated seconds):

| Runner | Passed | Calls | Server 429s | Admission p95 s | Mean ready queue | Completed tasks/s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Uncoordinated | 12/24 | 60 | 48 | 7.808 | 0.822 | 0.1912 |
| Coordinated | 24/24 | 24 | 0 | 310.336 | 11.949 | 0.0771 |

Successful throughput was 0.0956 versus 0.0771 tasks/s over different finite
completion horizons. Rapid failures inflate the baseline's completed throughput;
this is a request-cost/recovery tradeoff, not a measured speedup. Ready queues
exclude backoff, so baseline retries waiting on server timers do not enter that
queue statistic. Peak initial ready backlog was 24 in both modes.

With first-accepted-attempt transient failures, coordinated runs passed 24/24
using 48 calls and 24 retries, with no server 429s. Baseline runs used 72 calls,
received 60 server 429s, and passed 0-3 tasks depending on scheduling. During
sustained outage, the coordinated breaker bounded calls to eight (two retries)
with 24 admission rejections; baseline used 72 calls and received 60 server 429s.
All three repetitions had those call/rejection counts. Raw generated artifacts
remain in `/tmp/forge-provider-limits-*-final-20261025.json`, outside Git.

## Earlier working milestones

`67417cf` minimal CLI; `b9c056a` suite validation/provenance; `66c188f` seeded
generation; `ed69fbe` tool fixture/replay; `476bc67` bounded thread runtime;
`f4ddb30` native HTTP deadlines; `4cfe3b6` jitter/fair retry scheduling;
`cdb7e5c` latency/error/retry benchmarks. Earlier synthetic-delay results remain
in Git history; they were not model/GPU performance claims.

## Next milestone: local open-weight inference and GPU baseline

Implement an explicitly configured OpenAI-compatible local endpoint adapter for
an existing vLLM server, using offline HTTP/SSE fixtures first. Then:

1. Record checkpoint revision/license, tokenizer/chat template, dtype, runtime
   version, launch arguments, GPU/driver/CUDA details, and task/prompt digests.
2. Capture streamed first-token and per-token timing, exact token counts, errors,
   cancellation, and end-to-end latency with shared resource controls disabled
   or reported explicitly during capacity sweeps.
3. Sweep concurrency and prompt/output lengths; compare Forge measurements with
   `vllm bench serve` under the same checkpoint/workload. Report token throughput,
   TTFT, inter-token latency, p50/p95/p99, GPU memory/utilization, and raw samples.
4. Only after profiling, choose a measured optimization (batching/cache/memory
   configuration). Add C++/Rust only if profiling identifies a justified benefit.

The [vLLM server](https://docs.vllm.ai/en/latest/serving/online_serving/) and
[benchmark CLI](https://docs.vllm.ai/en/latest/cli/bench/serve/) provide integration
and comparison points. [PagedAttention](https://arxiv.org/abs/2309.06180) motivates
studying KV-cache memory and batching; its published results are not Forge
results. This milestone is not implemented. Use existing authorized hardware;
paid GPU provisioning/model-service calls still require explicit approval.
