# Forge progress

## Owner-run local GPU baseline (2026-10-26)

On the owner's Windows PC via WSL2, vLLM 0.32.0 served
`Qwen/Qwen2.5-Coder-1.5B-Instruct` at checkpoint/tokenizer revision
`2e1fd397ee46e1388853d2af2c993145b0f1098a`, Apache-2.0, bfloat16. Hardware:
RTX 5070 Ti (16,303 MiB), Windows driver 610.88, 31 GB system RAM. Launch
settings included max model length 4096, max sequences 8, GPU memory utilization
0.70, and `--enforce-eager`. FlashAttention 2 was selected. FlashInfer was
unavailable but not required for startup. The model endpoint returned a valid
chat completion with provider-reported token usage.

Forge fixture validation accepted 6/6 tasks and caught 6/6 known mutations. A
real local model evaluation passed 4/6 tasks with zero provider errors. The
subsequent endpoint sweep completed 54 requests (six tasks, concurrency 1/2/4,
three repeats each), again 4/6 per sample with zero provider errors and retries.
Owner-reported median completed-task throughput was 12.667, 25.186, and 41.234
tasks/s at concurrency 1, 2, and 4. Each sample contains only six tasks, so its
nearest-rank p95/p99 equal its maximum and are weak tail estimates. The sweep
artifact remains on the PC at `~/forge/qwen-gpu-sweep.json`; it is not committed.
This initial run did not collect GPU/server telemetry and is not a comparison
against vLLM's benchmark client or an optimized runtime baseline.

## Optional telemetry increment (2026-10-26)

The local sweep accepts `--collect-telemetry` to sample fixed-query
`nvidia-smi` output once per second and capture selected vLLM Prometheus samples
before and after the sweep. Collection is best-effort: unavailable utilities,
metrics, or fields are recorded as unavailable; subprocess lifetime, row count,
metrics body size, and scrape deadline are bounded. Metrics labels are preserved
and histogram buckets/counters remain snapshots, not fabricated per-request
timings. Default sweeps remain unchanged and emit null telemetry. The full suite
passes 154 tests, including offline parser, option, serialization, and cleanup
tests. This Mac run did not access the owner's Windows GPU or exercise live vLLM
telemetry.

## Local inference increment (2026-10-26)

Added schema-2 local endpoint configuration with operator-declared model,
checkpoint/tokenizer revisions, license, dtype, and server version. Native local
JSON inference reuses pooled HTTP, deadlines, retry classification and provider
coordination. Only numeric loopback URLs under `/v1` are accepted; connections
require `--allow-network`. Local requests do not read/send OpenRouter credentials.
Legacy configs and synchronous providers remain supported. Local runs retain
reported usage through coordinated evidence, with quotas/breaker disabled by
default (the retry budget remains bounded).

`06b20b5`: 136 tests passed, 0 failures/errors, including actual loopback evaluation and
usage/report roundtrip. No model server, model download or GPU experiment was
run. Next in this slice: bounded SSE parsing, content-arrival instrumentation,
and a reproducible local endpoint concurrency sweep.

## Streaming increment (2026-10-26)

Added bounded incremental SSE parsing (UTF-8 fragments/BOM, CR/LF/CRLF, comments,
multiline data), validated finish/[DONE] handling, and usage-only final events.
Local JSON/stream responses retain prompt/output/total usage and response model;
unexpected model IDs fail. Content arrival offsets are monotonic and explicitly
measured per nonempty content delta, not per token. No partial stream is returned
as a successful completion after EOF, deadline, or cancellation.

Local runtime v2 attempts optionally include validated inference evidence; old
reports remain supported. Reservation estimates include the configured output
cap and largest suite prompt. The example enables streaming with fixed seed 0
and temperature 0. 144 tests passed, 0 failures/errors, including stream/report
roundtrip, malformed/tampered evidence, bounded parsing, and cleanup tests.
`05aaebb`: streaming and evidence increment committed. No actual model/GPU
measurements have been performed.

## Endpoint sweep increment (2026-10-26)

Added a library-backed local endpoint sweep with rotated concurrency order,
explicit consent, bounded planned calls, no warmups/retries/quota throttles, raw
evaluation reports, source hashes, and declared model/runtime provenance. Metrics
include content-arrival percentiles, usage coverage and reported output-token
throughput. Unknown usage and uncollected GPU telemetry are null. Session ownership
now lives outside the CLI; existing imports and commands remain compatible.

Final suite: 148 tests passed, 0 failures/errors. Sweep tests used a trusted
loopback fixture, not model inference. See `docs/local-inference.md` and
`benchmarks/local_inference.py`. The next hardware step remains server/GPU
telemetry and token-aligned timing against a pinned vLLM checkpoint. The local
adapter and measurement harness are implemented; actual GPU baselines are pending.

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

## Next milestone: correlate Forge and serving-engine measurements

The local adapter and first real model sweep are complete. Optional one-second
`nvidia-smi` sampling and before/after vLLM Prometheus snapshots are implemented
in the local sweep through `--collect-telemetry`. Next, rerun the pinned
checkpoint with telemetry, then compare the same prompts and concurrency against
[vLLM 0.32.0 bench serve](https://docs.vllm.ai/en/v0.32.0/cli/bench/serve/).
Add token-aligned timing and control cache/warmup settings before interpreting
throughput or latency. The [SSE standard](https://html.spec.whatwg.org/multipage/server-sent-events.html#event-stream-interpretation)
guides framing; it does not establish token boundaries. Hardware details and
server metadata remain operator-supplied, not independently attested. Use
existing authorized hardware; paid GPU provisioning/model-service calls still
require explicit approval.
