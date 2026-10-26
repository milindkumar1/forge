# Forge

A small evaluation CLI: run a provider against a validated development task suite,
save the evidence, and inspect a JSON report. Six built-in cases provide an offline
starting point. This slice exercises evaluation
plumbing, not coding-agent capability or the project's research hypothesis.

## Install and run

Python 3.9 or newer. There are no runtime dependencies.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools
.venv/bin/python -m pip install -e .
.venv/bin/forge init
.venv/bin/forge validate
.venv/bin/forge run
.venv/bin/forge report
.venv/bin/forge report --json exported-report.json
```

`init` writes `forge.json`. `run` reads it and writes `forge-results.json`.
All commands accept explicit paths (`--config`, `--output`, or `--input`);
relative paths resolve from your current directory. Writes refuse to overwrite
existing files: use a new `--output` for each run.

The default config is entirely offline:

```json
{
  "schema_version": 1,
  "provider": "mock",
  "model": "mock-v1",
  "mock_mode": "correct"
}
```

The mock computes reversals, whitespace word counts, and numeric sorts using
trusted built-in operations. Change `mock_mode` to `faulty` to exercise failed
evaluations. Exit codes: `0` for successful commands/all tasks passing, `1` for
a completed run with failed tasks (including provider errors), `2` for input,
configuration, or file errors. `report` returns `0` when it successfully reads
a valid report, even if that saved run contains failures.

## Generate development tasks

After `forge init`, generate and evaluate a reproducible task file:

```sh
.venv/bin/forge generate --seed 42 --count 12
.venv/bin/forge validate --tasks forge-tasks.json
.venv/bin/forge run --tasks forge-tasks.json --output generated-results.json
.venv/bin/forge report --input generated-results.json
```

`generate` needs no config, provider, or network access. It writes
`forge-tasks.json` by default, or a new path supplied with `--output`, and refuses
to overwrite existing files. Defaults are `--seed 0 --count 12`. Seeds must be
integers from `0` through `4294967295`; counts must be integers from `3` through
`1000`. Generation or file errors return exit code `2`.

The first three cases cover non-palindrome reversal, irregular whitespace
counting, and numeric sorting with negatives and duplicates. Counts of six or
more also include an empty reversal, empty word count, and singleton sort. Extra
cases vary the same bounded templates. Within a suite, no two tasks have the
same operation and input. Duplicate candidates are resampled, with a maximum of
100 attempts per task. The completed suite must pass the existing reference and
mutation checks before it is written; failed validation creates no output file.

The existing `source` field records a recipe such as:

```text
generator:forge/text-templates-v1?seed=42&count=12&python=3.9.6&runtime=CPython
```

It identifies the generator version, seed, count, and Python runtime. With the
same generator and recorded runtime, the same recipe produces identical task
file bytes and a stable digest. No timestamp or output path is included.
Reproduction across Python versions is not promised; preserve the generated
file and the report's embedded snapshot. Generation uses its own random stream
and leaves process-global randomness untouched.

These are generated **task data**, not generated executable code or LLM-created
tests. Different seeds can overlap and share the same templates. They do not
establish independent training/evaluation splits or demonstrate generalization
beyond the known mutation checks.

## Concurrent text evaluation

The sequential runner remains the default. Opt into the original thread-backed
runner for built-in or generated text suites:

```sh
.venv/bin/forge run --concurrency 4 --output concurrent-results.json
.venv/bin/forge report --input concurrent-results.json
```

`--concurrency` accepts 1-64 workers. The runner uses asyncio and a dedicated
thread pool to overlap blocking provider calls. There are at most that many
active rollouts and submitted calls; results retain suite order. Providers used
through the Python API must be thread-safe. `run-tool` remains sequential.

Retries default to one attempt. With `--concurrency`, optionally set
`--max-attempts 3 --retry-delay 0.25`. Attempts are bounded to 1-5 total per task;
initial delay must be finite in [0, 60] seconds. Exponential backoff is
`min(60, initial_delay * 2**(attempt_number - 1))`, without jitter, and holds the
rollout's worker slot. Only `RetryableProviderError` triggers another attempt.
OpenRouter classifies HTTP 429/500/502/503/504, URL transport failures, timeouts,
and connection failures as retryable. Authentication errors, malformed responses,
non-text responses, and wrong answers do not retry. Retried requests can incur
additional charges, including after an ambiguous transport failure. Network
access still requires explicit `--allow-network`; no live calls were used here.
This compatibility runner has no server-directed delay or jitter; the native
runner below adds both. Provider-wide admission controls are available separately
on the native runner. Request deduplication is not implemented.

Schema-1 reports add `runtime` settings, total attempts, peak submitted requests,
elapsed time, and completed tasks/second. Per-task evidence adds initial queue
wait and attempt outcomes/durations. Task duration includes retries and backoff
but excludes initial queue wait; attempt duration includes thread dispatch and
the provider call. Runtime elapsed time includes pool setup/cleanup, excluding
suite validation and report assembly. Throughput counts all completed tasks,
including failures. The reader checks attempt and metric consistency while
retaining support for older reports.

Cancellation and unexpected programming errors stop admission and join running
threads before propagating; no partial report is written. Cleanup may block the
event loop until those calls finish. The provider owns I/O timeouts (OpenRouter
uses a 30-second socket timeout, not an end-to-end deadline). A running Python
thread cannot be forcibly cancelled. This is a local I/O concurrency runtime;
it has no durable recovery, batching, or distributed workers.

## Native asynchronous HTTP and retry scheduling

The mock path needs no dependencies. Install the optional HTTP extra for OpenRouter:

```sh
.venv/bin/python -m pip install -e '.[http]'
.venv/bin/forge run --native-async --concurrency 4 --request-timeout 30 \
  --max-pending-retries 16 --max-attempts 3 --retry-delay 0.25 \
  --retry-seed 42 --max-retry-after 300 --output native-results.json
```

This runs offline with the default mock config. An OpenRouter config also
requires `--allow-network` and the environment API key, and may incur charges.
The synchronous `complete()` API, default CLI behavior, and thread runner are
preserved. The new Python API uses `AsyncProvider.acomplete()` and
`evaluate_native()`; `AsyncOpenRouterProvider` requires an `async with` scope
to own and close its pooled aiohttp session.

`--request-timeout` is a whole-request deadline in (0, 3600] seconds (default 30),
covering connection-pool acquisition, headers, and bounded body reading. It starts
at request admission and excludes queueing and backoff. Expiry cancels and awaits
the request before releasing its active slot. External cancellation propagates
and cancels/awaits all active requests; pending timers are discarded. Custom async
providers must cooperate with cancellation; no runtime can forcibly interrupt
Python code that blocks the event loop or suppresses cancellation.

`--concurrency` bounds active requests (default 1 for native mode). Independently,
`--max-pending-retries` bounds delayed and ready retries (0-1000, default 64).
A central timer heap releases request capacity during backoff; it does not create
a sleeping task per retry. When fresh tasks and due retries are both ready,
admission alternates between the two queues. Fresh tasks retain suite order;
retry order is by ready time, then insertion order. Full retry queues produce
`retry_queue_full` terminal failures instead of allocating more memory.

The native retry policy uses **full jitter**:
`delay = max(Retry-After, uniform(0, min(60, base_delay * 2**(attempt - 1))))`.
Numeric and HTTP-date Retry-After values are supported for transient HTTP
responses including 429; malformed/past values add no minimum wait. The random
seed is local to each task/attempt, so the sampled delay is independent of
completion order. Seeds reproduce sampled delays, not network timings or the
set of overloaded tasks. A server wait beyond `--max-retry-after` (default 300,
allowed (0, 3600]) produces `retry_after_limit`; the scheduler never shortens the
server minimum into an early retry. Without coordination, backoff is per request
and does not pause fresh requests globally after a 429. Attempts remain bounded
to 1-5, default 1.

Uncoordinated native reports use runtime kind `native-async-v1` within report
schema 1. They
record timeout flags, retry floors/delays, termination reasons, peak active and
pending counts, total attempts, executed retries, and refused retries. Saved
runtime evidence is validated on load; it is not an authentication mechanism.

## Local inference endpoint

Copy [the local config example](examples/local-vllm.json), fill its model/runtime
provenance from your existing server, then run:

```sh
.venv/bin/forge run --native-async --config local.json --allow-network --output local-results.json
```

This connects only to an explicitly configured numeric loopback endpoint under
`/v1`. It does not launch a server, download weights, or use an OpenRouter key.
The example contains placeholders, not a measured model configuration. Declared
revisions/license/dtype/runtime are operator provenance, not server attestation.
Local runs use coordinated evidence to retain reported token usage; without
explicit coordination settings they impose no RPM/TPM limits or circuit breaker.

## Shared provider admission and failure controls

Enable coordination with `--coordinate` or a quota/failure setting. These require
`--native-async`; existing synchronous, thread, and native paths remain compatible.
With the default mock config this stays offline:

```sh
.venv/bin/forge run --native-async --concurrency 4 --requests-per-minute 60 \
  --tokens-per-minute 10000 --tokens-per-request 512 --retries-per-minute 8 \
  --max-attempts 3 --output coordinated-results.json
```

Shared rolling RPM/TPM budgets are checked before allocating an active request
slot. Every dispatched retry charges capacity again. Token reservations reconcile
optional reported usage; failures/cancellations retain unknown usage. The default
reservation is an estimate, not model tokenization. Provider-wide Retry-After
cooldowns use monotonic scheduling. A bounded retry budget and circuit breaker
limit sustained failures; FIFO tickets preserve fairness across shared runs.

Python callers can share a `ProviderCoordinator` across `evaluate_native()` calls
on one event loop for the same quota scope. Coordinated runtime reports use
`native-async-v2`, with explicit admission rejection, usage, wait, and queue data.
See [provider admission](docs/provider-admission.md) for settings, accounting,
breaker recovery, scope, and limitations.

## Runtime benchmarks

Run the reproducible offline experiment:

```sh
.venv/bin/python benchmarks/runtime.py --output /tmp/forge-runtime-benchmark.json
```

Synthetic mode compares sequential, thread-backed (1/4), and native async (1/4)
scheduling over 24 seeded tasks with a 10 ms sleep, three rotated repetitions,
and offline warmups. The native mock uses `asyncio.sleep`; this measures scheduler
behavior, not the HTTP transport. Add `--transient-every 3` to fail the first
attempt of every third task. Concurrent modes get two attempts with zero backoff;
the original sequential baseline has one attempt. Failure-injection runs compare
recovery behavior under these different policies, not equal-work speedups.

Benchmark schema 2 records completed/successful tasks per second, final task
error/failure rates, attempt error rate, executed retry counts, and nearest-rank
p50/p95/p99 latencies. Attempt latency measures each request. Task latency includes
retry waits but excludes initial admission queueing; completion latency includes
that queueing and is null for the sequential baseline, which does not record it.
Wall time includes validation, report construction, and session setup/cleanup.
Percentiles are per sample; with 24 tasks, p99 is the maximum. Raw samples,
environment, and suite digest are saved. Artifacts refuse overwrite.

Real API measurements require a separate mode and output, an OpenRouter config,
and explicit paid-network opt-in:

```sh
.venv/bin/python benchmarks/runtime.py --mode api --config openrouter.json \
  --allow-network --count 6 --repeats 1 --concurrency 2 --max-attempts 1 \
  --output /tmp/forge-real-api.json
```

API mode performs no warmup calls or synthetic delays/failures and saves complete
run reports. It measures remote inference, routing, networking, and local overhead
together; it cannot isolate GPU performance. Outputs are tagged `synthetic` or
`real_api` and cannot mix modes. No real API measurements were run for this change.
Local synthetic results and next steps are in [PROGRESS.md](PROGRESS.md).

Compare admission behavior under a **simulated** provider quota:

```sh
.venv/bin/python benchmarks/runtime.py --mode limits --output /tmp/forge-limits-clean.json
.venv/bin/python benchmarks/runtime.py --mode limits --scenario transient --output /tmp/forge-limits-transient.json
.venv/bin/python benchmarks/runtime.py --mode limits --scenario outage --output /tmp/forge-limits-outage.json
```

Defaults: 24 tasks, three repetitions, 4 RPM, 12 TPM, three exact synthetic tokens
per accepted request, instantaneous service, and time scale 600 (a simulated
minute takes 0.1 real seconds). Modes compare equal concurrency and per-task
attempt bounds. Artifacts are tagged `synthetic`, with metrics in **simulated
seconds** and real wall duration recorded separately. They report admission
p50/p95/p99, peak/mean ready-queue depth, admission/provider rejections, retry
counts, and completed/successful throughput. These are not model/GPU/HTTP
performance measurements. See [benchmark semantics](docs/provider-admission.md#synthetic-limit-experiment)
and [measured observations](PROGRESS.md).

## Run a bounded tool-use episode

```sh
.venv/bin/forge run-tool
.venv/bin/forge report --input forge-tool-results.json
```

`run-tool` reads the same `forge.json` config and defaults to the offline mock.
It accepts `--config`, `--output`, and the same opt-in `--allow-network` gate as
`run`. Outputs are never overwritten. Exit codes are `0` for a passing episode,
`1` for a completed failed episode, and `2` for configuration or file errors.

The public development task asks for the quantity of washers in a small
inventory. Each provider turn returns JSON requesting
`{"tool":"read_fixture","path":"inventory.json"}` or a final
`{"answer":"7"}`. Forge dispatches the single trusted tool against an in-memory
fixture map. It never opens a provider-supplied path or executes provider code.
The initial prompt lists the available file and question without its contents
or expected answer. Subsequent prompts include the recorded interaction history.
The mock derives its answer from the tool result.

A pass requires a successful read followed by the exact answer within three
provider turns. Unknown paths produce tool errors and consume a turn; malformed
actions terminate the episode. Responses over 10,000 characters terminate it
and are omitted from the trace to bound stored evidence. Provider errors are
sanitized. `faulty` mock mode emits a malformed action and fails.

Tool reports use schema version `2` and include the versioned fixture snapshot,
its SHA-256 digest, prompts, responses, tool results/errors, final answer, and
termination reason. `report` replays recorded responses with the trusted fixture
to check trace and verdict consistency, without inference. It supports this
specific fixture version; text reports retain schema `1` and remain readable.
Replay checks consistency, not authenticity. This fixed public fixture is an
integration test, not evidence of agent capability or held-out generalization.
The protocol uses JSON text through `complete`, not native API tool calling.
With OpenRouter it makes at most three sequential requests; live compatibility
has not been verified and token/cost accounting remains unimplemented.

## Load and validate task files

Task suites are JSON data files. Use the checked-in example as a starting point:

```sh
.venv/bin/forge validate --tasks examples/text-dev-v1.json --json validation.json
.venv/bin/forge run --tasks examples/text-dev-v1.json --output custom-results.json
.venv/bin/forge report --input custom-results.json
```

`validate` runs entirely offline and needs no config or provider. It checks each
expected answer against a trusted reference implementation, confirms that each
`known_wrong` answer is wrong, and checks whether the cases catch every shipped
mutant for the operations in the suite. A mutant is a deliberately faulty
implementation, such as lexical numeric sorting or dropping duplicate numbers.
The validation JSON identifies which task IDs catch each mutant. Correct labels
alone are insufficient: a palindrome-only reversal suite cannot catch a provider
that simply returns its input. Such a suite is rejected.

Validation exits `0` for an accepted suite, `1` for failed reference/mutation
checks, and `2` for malformed files or file errors. Diagnostic JSON can be saved
even when checks fail. `run` performs the same preflight; an invalid suite returns
`2` before provider creation or inference, without creating a results file.
Existing evidence files are never overwritten.

The task document fields are:

| Field | Contract |
| --- | --- |
| `schema_version` | Integer `1` |
| `id` | Stable suite identifier, 1–80 characters |
| `split` | `development`; other splits are rejected in this slice |
| `source` | Nonempty provenance description, up to 1,000 characters |
| `tasks` | Array of 1–1,000 tasks, in execution order |

Every task has exactly `task_id`, `operation`, `text`, `expected`, and
`known_wrong`. IDs start with a letter or digit and contain only letters, digits,
dots, underscores, or hyphens; task IDs are unique within a suite. The three
operations are `reverse`, `word_count`, and `sort_numbers`. Text and answer fields
must be strings of at most 10,000 characters. Numeric sort inputs contain
comma-separated signed integers with at most 18 digits each and no spaces;
outputs use canonical decimal spelling. Reversal uses Unicode code points;
word counting uses Unicode whitespace. Task files are capped at 1 MB and unknown
fields are rejected. See [the complete example](examples/text-dev-v1.json).

Reference and mutant implementations are trusted code shipped with Forge.
Suite files contain no executable verifier or provider code. The `source` field
records the author's provenance claim; Forge does not authenticate that claim.
Mutation coverage checks a finite list of known bugs, not all possible errors.

## Report contents

Each report includes schema/tool versions, UTC start time, Python/platform,
the resolved config, a complete suite snapshot and SHA-256 digest, validation
evidence, and per-task prompts, expected
answers, actual answers, verdicts, provider errors, and elapsed seconds. Its
summary includes total, passed, failed, error count, and pass rate. Provider
errors are a subset of failures. Answers and verdicts are repeatable for the
mock; timestamps and timings vary. Reports include the development answers,
so they must never serve as independent held-out evaluation material.

Suite `digest_version: 1` hashes canonical JSON containing the complete task
document, including metadata and task order. Formatting and JSON key order do
not affect it. Earlier reports hashed only the built-in task list; those reports
have no digest version and remain readable. New reports are checked against
their embedded snapshot, including answer/verdict consistency. Hashes detect
inconsistency, not malicious rewrites or authenticity.

## Optional OpenRouter

Set `provider` to `openrouter` and `model` to an explicit OpenRouter model ID in
the config. Supply `OPENROUTER_API_KEY` in your environment, then run:

```sh
.venv/bin/forge run --allow-network --output openrouter-results.json
```

Both the flag and environment key are required. By default this sends one sequential API
request per task (six for the built-in suite)
and may incur charges. `--concurrency` enables overlapping requests and optional
bounded retries as described above. The adapter has offline
tests using a fake HTTP transport; live compatibility has not been verified.
The report records the requested model ID, not a resolved model revision or
provider routing. Temperature zero does not guarantee remote reproducibility.
Token usage and cost accounting are not implemented. Credentials are never
included in config or reports; keep `.env` files out of Git.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover the full CLI path, repeatable mock outcomes, known wrong answers,
strict output verification, report consistency, malformed inputs, overwrite
protection, and mocked OpenRouter requests and failures. No paid calls or
generated code execution occur. Suite tests check schema failures, independent
reference answers, named mutant detection, rejection before inference, digest
stability, custom-suite runs, snapshot consistency, and legacy report reading.
Generator tests cover replay, provenance, duplicate detection, input bounds,
corrupt-answer rejection, maximum-size suites, and the full offline
generate → validate → run → report workflow.
Tool episode tests cover successful and incorrect answers, mandatory reads,
path rejection without host access, malformed actions, turn and response limits,
sanitized errors, trace replay, overwrite protection, and CLI network gating.
Native tests use events and a virtual retry clock to check request/retry limits,
fairness, overload, seeded jitter, deadlines, and cancellation. With the HTTP
extra installed, transport tests also use a loopback-only server for a real
429/retry exchange and deadline-triggered socket cleanup. Restricted sandboxes
must permit binding a temporary loopback socket. Without the extra, HTTP tests
are skipped; install it to run the complete suite.

## Scope and next step

These six public development fixtures are deliberately small. Exact string
matching penalizes extra formatting. The mock is a plumbing baseline, and its
pass rate is not research evidence. LLM-generated tests, sandboxed coding
episodes, independent held-out datasets, training, distributed scheduling, and a website
are not implemented.

The next milestone is an OpenAI-compatible local vLLM adapter with pinned
open-weight inference, streaming token timings, concurrency sweeps, and GPU
telemetry when hardware is available. Acceptance criteria are in [PROGRESS.md](PROGRESS.md).
The tool-use track also needs a second fixture to test its episode contract.
See [the architecture note](docs/architecture.md) and
[the project brief](project_brief.md).
