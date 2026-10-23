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

Both the flag and environment key are required. This sends one sequential API
request per task (six for the built-in suite)
and may incur charges. There are no retries. The adapter has offline
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

## Scope and next step

These six public development fixtures are deliberately small. Exact string
matching penalizes extra formatting. The mock is a plumbing baseline, and its
pass rate is not research evidence. LLM-generated tests, sandboxed coding/tool-use
episodes, independent held-out datasets, training, scheduling, and a website
are not implemented.

The next small step is one fixture-backed tool-use task with explicit tool-call
and result traces, extending the evaluation loop beyond text transformations.
See [the architecture note](docs/architecture.md) and
[the project brief](project_brief.md).
