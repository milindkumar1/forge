# Generation, validation, and evaluation

The data flow is `CLI -> config -> provider -> evaluation -> JSON -> report`.
The CLI owns paths, explicit network consent, exit codes, and presentation.
Config parsing rejects unsupported schemas and unknown fields rather than
silently ignoring misspellings. JSON writes use exclusive creation to preserve
existing evidence and configs.

`tasks.py` contains immutable built-in development fixtures with stable IDs, public
inputs, and private-to-the-provider expected answers. The prompt contains the
operation contract and input, never the expected answer. A strict equality
verifier checks returned strings. `suites.py` adds a versioned JSON document,
an immutable suite object, and offline quality checks. It accepts only data for
three trusted operations; task files cannot supply executable verifiers. The
built-in suite and external files follow the same validation path. Only the
development split is accepted, avoiding an unsupported promise of held-out
isolation simply because a file claims a different split.

A versioned suite digest covers metadata, inputs, expected answers, known wrong
answers, and execution order. Canonical JSON makes formatting changes irrelevant.
The report embeds the complete suite and validation evidence, so source-file
edits do not change the saved record. Earlier reports retain their legacy
metadata-only format and remain readable; new report fields are additive.
No held-out or training dataset exists yet; these fixtures are development-only.

`generation.py` constructs development suites using a local `random.Random`
instance and bounded templates. It guarantees coverage-bearing cases for the
three operations before adding boundary cases and further varied inputs. Task
IDs reflect execution order; operation/input pairs are unique within each suite.
Duplicate candidates are resampled with a bounded retry count. This retry is
only for duplicates: generation never silently edits the validation policy or
retries a rejected suite until its mutation score happens to pass.

The generator builds labels from template inputs rather than reading the
validator's reference outputs. Generated suites still pass through the same
independent references and mutation gate as handwritten suites, before any file
is created. The code intentionally shares simple operations with the mock;
mock success is an integration check, not independent capability evidence.

The existing provenance string uses a `generator:` URI with generator version,
seed, count, Python version, and runtime implementation. This keeps task schema
1 unchanged while making the recipe machine-readable. Timestamp, local paths,
and process-global random state do not enter the recipe. Generator behavior
changes require a new generator version; Python runtime changes may also change
sampling. Snapshots remain the authoritative record of the generated cases.

The central generation tradeoff is predictable, bounded templates versus broader
task diversity. These templates are deliberately designed to catch known bugs,
so high mutation coverage is not evidence of broad evaluator quality. A new seed
changes the sample within the same distribution; it does not create an
independent held-out benchmark or guarantee disjoint task inputs.

The provider protocol exposes just `complete(prompt) -> str`. The deterministic
mock computes answers independently of the expected-answer table. OpenRouter
uses the same boundary, with opt-in network access and credentials read only
from the environment. Expected provider failures become sanitized per-task
errors so a failed request still produces useful run evidence. Unexpected
programming errors propagate instead of being misreported as model failures.

Both `run` and `validate` check file structure and bounded input size. Validation
first recomputes every expected answer using trusted references separate from
the provider. For example, numeric sorting uses insertion rather than the mock's
`sorted()` implementation. This reduces shared implementation errors; it does
not prove the reference code is correct. Known wrong answers are checked against
the recomputed references. If reference checks fail, mutation scoring is skipped
so invalid labels cannot produce misleading mutation evidence.

For each operation present in the suite, all shipped mutants must be caught by
at least one task. Evidence names every mutant and the tasks that caught it.
Missing operations are not scored: a reversal-only suite need not test sorting.
This is a quality gate over known mistakes, not a claim of comprehensive
correctness or a code-coverage metric. The validator version identifies the
reviewed reference/mutant policy; a policy change should change that version.
Validation rejects weak suites instead of silently changing their task mix.

The CLI validates before constructing the provider, so invalid tasks cannot
trigger paid inference. The evaluator also validates direct Python callers,
preserving the same guarantee before any provider call. This repeats a small
preflight in the CLI path in exchange for protecting both entry points.

The default evaluator runs tasks sequentially and owns verification; a provider cannot
choose its own pass/fail verdict. Timings measure the complete provider call,
including response parsing, using a monotonic clock. They are not token or GPU
performance measurements. The report reader checks result statuses, unique
task IDs, and summary consistency. New reports also check the embedded suite
digest, result order, prompts, expected answers, and answer/verdict consistency.
The reader does not re-run current mutation policy against historical evidence;
the saved validator version identifies what was checked at the time. It is not
a cryptographic authenticity check or a full arbitrary-report schema validator.

The central tradeoff is strict string verification versus a richer execution
environment. String tasks make the first loop safe, dependency-free, and easy
to debug, but cannot evaluate repository edits or tool-use trajectories. Model
output is always data; nothing calls `eval`, `exec`, or a shell with that output.
Real coding tasks require a separately designed sandbox before execution.

`tool_episode.py` adds a separate bounded episode through `forge run-tool`.
It reuses `complete(prompt) -> str`, asking for a JSON action each turn and
including the interaction history in subsequent prompts. One trusted
`read_fixture` operation reads an exact allowlisted name from in-memory data;
there is no host filesystem access or generated code execution. Three turns
bound repeated calls; malformed actions stop immediately. Unknown paths produce
recorded errors so a provider can recover within its remaining budget. Passing
requires both a successful read and the exact final answer. This checks an
observable interaction requirement; it cannot prove the model reasoned from
the tool result rather than memorizing the public fixture.

Tool reports use schema 2 with a versioned fixture snapshot and digest. The
reader replays saved responses through the same state transitions and compares
prompts, tool results, termination, answer, verdict, and summary. Oversized
responses are represented by a null response and an invalid-action event;
their content is deliberately not preserved. Replay validates internal
consistency and does not authenticate provider origin. Fixture version 1 is
fixed in code; a future fixture revision needs an explicit compatibility policy.
The existing text suite schema and report reader remain supported.

## Bounded asynchronous text runner

`runtime.py` adds an opt-in asyncio scheduler over the same blocking provider
protocol. A dedicated `ThreadPoolExecutor` and a fixed set of coroutine workers
both have the configured concurrency bound. Workers pull from one iterator on
the event-loop thread, then await one submitted request at a time. Only
O(concurrency) coroutine tasks and provider futures exist; the suite and ordered
result buffer remain O(task count). No semaphore hides an unbounded executor
queue. Callers supplying a custom provider must make concurrent calls safe.

Task indices choose result slots, so completion order cannot change suite order
or provenance. Verification stays in the evaluator. A shared report builder
keeps the sequential and concurrent evidence contracts aligned. Additive runtime
fields retain schema-1 readability and are checked on load. Timings use a
monotonic clock; peak in-flight counts submitted requests, not measured server
activity. Completed-task throughput includes failed tasks and excludes validation.

Only a `RetryableProviderError` allows retry. OpenRouter maps selected HTTP
statuses and transport errors to that subclass; the base error remains permanent.
Malformed output and wrong answers do not retry. The default attempt budget is
one; explicit retries use capped exponential backoff while retaining the worker
slot. This sacrifices some utilization during an outage to keep admission and
retry pressure bounded. Backoff currently has no jitter or server-directed delay.

Unexpected errors propagate. All worker coroutines are cancelled and awaited,
then the executor joins any running calls. Cancellation follows the same path,
so a run never leaves its provider threads running after it returns. Joining can
block the event loop during failure cleanup. Providers must bound their own I/O;
socket timeouts do not establish an end-to-end deadline. This bridge avoids a
new HTTP dependency while preserving existing implementations, at the cost of
thread resources and delayed cancellation. The native path below provides
whole-request deadlines and cooperative cancellation without the thread bridge.

Primary references: Python's [executor integration](https://docs.python.org/3.9/library/asyncio-eventloop.html#executing-code-in-thread-or-process-pools)
and [executor shutdown semantics](https://docs.python.org/3.9/library/concurrent.futures.html#concurrent.futures.Executor.shutdown).
Running futures cannot be cancelled; the cleanup design follows that established
constraint. The synthetic benchmark checks overlap on this host. Whether this
improves real model throughput remains an untested hypothesis; batching, server
capacity, and rate limits can dominate real workloads. No distributed execution
or GPU performance claim follows from the experiment.

## Native HTTP and fair retry admission

`async_http.py` is optional (`forge-evals[http]`) and owns one aiohttp session
per run. `AsyncProvider.acomplete()` is separate from `Provider.complete()`, so
existing providers and callers keep their contracts. The native transport bounds
response bodies to 1 MB, disables redirects and environment proxy discovery,
and never serializes credential-bearing exceptions or HTTP error bodies.

`asyncio.wait_for` covers the whole exchange, including connector acquisition,
headers and streaming body reads, rather than resetting a timeout per chunk.
The scheduler also enforces an admission-time request deadline for custom async
providers. Cancellation must be cooperative: cleanup is awaited before another
request uses the slot. The HTTP session is closed after scheduler cleanup. A
deadline excludes retry delays and initial queueing; it is not a rollout deadline.

`native_runtime.py` owns two independent bounded structures: at most C active
request tasks and a timer heap of at most P pending retries (including ready
retries waiting for capacity). Fresh tasks are drawn from the existing bounded
suite. There is no per-task sleeping coroutine. Heap insertion costs O(log P);
state, ordered results, and attempt evidence use O(task count * attempt budget).
When both classes are ready, admissions alternate between fresh tasks and due
retries. Fresh order is suite order; retry order is ready time then insertion
sequence. This prevents starvation in either class without a promise about
remote completion order. Failures are harvested in task-index order when they
are observed simultaneously, making overload choices stable within that batch.

The retry delay is `max(server_floor, U(0, min(60, base * 2**(attempt-1))))`.
Per-task/attempt random streams make jitter samples independent of completion
order; actual timing and retry overflow still depend on scheduling. This is
full jitter, not multiplicative jitter around a fixed delay. Numeric and HTTP-date
Retry-After values establish a minimum wait. Delays exceeding policy terminate
explicitly instead of being capped downward. Pending-queue overflow sheds the
retry as a recorded task error. This sacrifices recovery under overload in
exchange for bounded memory and retry pressure. Fresh requests are not globally
paused after a 429; provider-wide rate coordination is future work.

The established protocol rule comes from [RFC 9110 Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after).
The client pooling/lifecycle follows [aiohttp documentation](https://docs.aiohttp.org/en/stable/client_quickstart.html#make-a-request).
The fair admission and overload policy are Forge design choices, verified with
controlled event and virtual-clock tests. They are not research claims about
optimal scheduling. A loopback HTTP fixture additionally verifies actual socket
exchange, a 429 retry, and disconnect on a stalled-body deadline. No live API
compatibility, GPU speedup, or distributed scaling result is claimed.

The benchmark keeps synthetic scheduling experiments and real API runs in
separate modes/artifacts. Attempt and task latencies are distinct; completion
latency includes admission queueing where recorded. Nearest-rank percentiles,
error/failure rates, and retry counts expose recovery costs even when final
success rates hide transient failures. Per-sample p99 over a small development
suite is descriptive, not a statistically stable tail estimate.

The tradeoff is a small JSON-text action contract versus native provider tool
calling. Reusing the provider boundary keeps this offline slice small and lets
the existing adapter participate, but it does not test native function-call
semantics. A second task is the next step before generalizing the episode runner.
A future training pipeline
must use separate task sources and an independently versioned held-out suite
with an explicit isolation workflow.
