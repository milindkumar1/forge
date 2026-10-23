# Forge

A small evaluation CLI: run a provider against six verifiable development tasks,
save the evidence, and inspect a JSON report. This first slice exercises evaluation
plumbing, not coding-agent capability or the project's research hypothesis.

## Install and run

Python 3.9 or newer. There are no runtime dependencies.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools
.venv/bin/python -m pip install -e .
.venv/bin/forge init
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

## Report contents

Each report includes schema/tool versions, UTC start time, Python/platform,
the resolved config, suite ID and SHA-256 digest, and per-task prompts, expected
answers, actual answers, verdicts, provider errors, and elapsed seconds. Its
summary includes total, passed, failed, error count, and pass rate. Provider
errors are a subset of failures. Answers and verdicts are repeatable for the
mock; timestamps and timings vary. Reports include the development answers,
so they must never serve as independent held-out evaluation material.

## Optional OpenRouter

Set `provider` to `openrouter` and `model` to an explicit OpenRouter model ID in
the config. Supply `OPENROUTER_API_KEY` in your environment, then run:

```sh
.venv/bin/forge run --allow-network --output openrouter-results.json
```

Both the flag and environment key are required. This sends six sequential API
requests and may incur charges. There are no retries. The adapter has offline
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
generated code execution occur. Verifiers also check a known wrong answer per
fixture before each run. This is a small mutation smoke check, not validation
of arbitrary generated tests.

## Scope and next step

These six public development fixtures are deliberately small. Exact string
matching penalizes extra formatting. The mock is a plumbing baseline, and its
pass rate is not research evidence. Generated tasks, sandboxed coding/tool-use
episodes, independent held-out datasets, training, scheduling, and a website
are not implemented.

The next small step is to strengthen verifier validation with independent
mutation cases and a versioned task-file format, before accepting generated
tasks. See [the architecture note](docs/architecture.md) and
[the project brief](project_brief.md).
