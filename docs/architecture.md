# First evaluation slice

The data flow is `CLI -> config -> provider -> evaluation -> JSON -> report`.
The CLI owns paths, explicit network consent, exit codes, and presentation.
Config parsing rejects unsupported schemas and unknown fields rather than
silently ignoring misspellings. JSON writes use exclusive creation to preserve
existing evidence and configs.

`tasks.py` contains immutable development fixtures with stable IDs, public
inputs, and private-to-the-provider expected answers. The prompt contains the
operation contract and input, never the expected answer. A strict equality
verifier checks returned strings. A suite digest covers inputs, expected answers,
and known wrong answers so fixture changes are visible in report provenance.
No held-out or training dataset exists yet; these fixtures are development-only.

The provider protocol exposes just `complete(prompt) -> str`. The deterministic
mock computes answers independently of the expected-answer table. OpenRouter
uses the same boundary, with opt-in network access and credentials read only
from the environment. Expected provider failures become sanitized per-task
errors so a failed request still produces useful run evidence. Unexpected
programming errors propagate instead of being misreported as model failures.

The evaluator runs tasks sequentially and owns verification; a provider cannot
choose its own pass/fail verdict. Timings measure the complete provider call,
including response parsing, using a monotonic clock. They are not token or GPU
performance measurements. The report reader checks result statuses, unique
task IDs, and summary consistency; it is not a cryptographic authenticity check
or a full arbitrary-report schema validator.

The central tradeoff is strict string verification versus a richer execution
environment. String tasks make the first loop safe, dependency-free, and easy
to debug, but cannot evaluate repository edits or tool-use trajectories. Model
output is always data; nothing calls `eval`, `exec`, or a shell with that output.
Real coding tasks require a separately designed sandbox before execution.

The fixture mutation smoke check proves only that each verifier accepts its
reference and rejects one named wrong answer. It does not establish broad test
quality. Independent mutation implementations and boundary cases should precede
generated task ingestion. A future training pipeline must use separate task
sources and an independently versioned held-out suite.
