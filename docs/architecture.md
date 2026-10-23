# Evaluation and task validation

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

The evaluator runs tasks sequentially and owns verification; a provider cannot
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

The next increment can generate task data from seeded, bounded templates and
reuse this gate. Generation should preserve source/version/seed provenance and
never weaken validation to force a candidate suite to pass. A future training
pipeline must use separate task sources and an independently versioned held-out
suite with an explicit isolation workflow.
