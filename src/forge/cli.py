"""The init -> run -> report command-line path."""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Optional

from forge.config import Config, LocalConfig, load_config, write_json
from forge.admission import AdmissionOptions
from forge.evaluation import evaluate, load_report
from forge.generation import generate_suite
from forge.providers import make_provider
from forge.native_runtime import NativeRuntimeOptions
from forge.inference_runtime import run_native
from forge.runtime import RuntimeOptions, evaluate_async
from forge.suites import DEFAULT_SUITE, load_suite, validate_suite
from forge.tool_episode import evaluate_tool


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="forge", description="Run small verifiable development evaluations.")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create an offline sample config")
    init.add_argument("--config", type=Path, default=Path("forge.json"))
    generate = commands.add_parser("generate", help="generate a validated development suite offline")
    generate.add_argument("--seed", type=int, default=0, help="random seed, 0-4294967295 (default: 0)")
    generate.add_argument("--count", type=int, default=12, help="number of tasks, 3-1000 (default: 12)")
    generate.add_argument("--output", type=Path, default=Path("forge-tasks.json"))
    run = commands.add_parser("run", help="evaluate a validated development suite")
    run.add_argument("--config", type=Path, default=Path("forge.json"))
    run.add_argument("--output", type=Path, default=Path("forge-results.json"))
    run.add_argument("--allow-network", action="store_true", help="allow configured inference connections (OpenRouter may incur costs)")
    run.add_argument("--tasks", type=Path, help="load a versioned JSON suite instead of built-in tasks")
    run.add_argument("--concurrency", type=int, help="use bounded async runner with 1-64 workers")
    run.add_argument("--max-attempts", type=int, default=1, help="attempts per task, 1-5; requires --concurrency")
    run.add_argument("--retry-delay", type=float, default=0.25, help="initial backoff seconds, 0-60; requires --concurrency")
    run.add_argument("--native-async", action="store_true", help="use native async provider and bounded retry queue")
    run.add_argument("--max-pending-retries", type=int, help="native: retry queue capacity, 0-1000 (default: 64)")
    run.add_argument("--request-timeout", type=float, help="native: whole-request deadline in seconds (default: 30)")
    run.add_argument("--retry-seed", type=int, help="native: reproducible full-jitter seed (default: 0)")
    run.add_argument("--max-retry-after", type=float, help="native: refuse longer server waits in seconds (default: 300)")
    run.add_argument("--coordinate", action="store_true", help="native: share provider admission, retry budget and circuit state")
    admission_flags = {
        "requests_per_minute": ("--requests-per-minute", int),
        "tokens_per_minute": ("--tokens-per-minute", int),
        "tokens_per_request": ("--tokens-per-request", int),
        "retries_per_minute": ("--retries-per-minute", int),
        "failure_threshold": ("--failure-threshold", int),
        "circuit_cooldown_seconds": ("--circuit-cooldown", float),
        "admission_timeout_seconds": ("--admission-timeout", float),
        "max_waiters": ("--max-admission-waiters", int),
    }
    for name, (flag, value_type) in admission_flags.items():
        run.add_argument(flag, dest=name, type=value_type, help="native provider coordination: " + name)
    tool = commands.add_parser("run-tool", help="run the bounded inventory tool-use fixture")
    tool.add_argument("--config", type=Path, default=Path("forge.json"))
    tool.add_argument("--output", type=Path, default=Path("forge-tool-results.json"))
    tool.add_argument("--allow-network", action="store_true", help="allow paid OpenRouter inference")
    validate = commands.add_parser("validate", help="check reference answers and mutation coverage offline")
    validate.add_argument("--tasks", type=Path, help="JSON suite; defaults to the built-in suite")
    validate.add_argument("--json", type=Path, dest="export", help="save validation evidence")
    report = commands.add_parser("report", help="summarize saved results and optionally export JSON")
    report.add_argument("--input", type=Path, default=Path("forge-results.json"))
    report.add_argument("--json", type=Path, dest="export")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            write_json(args.config, Config().to_dict())
            print("Created " + str(args.config))
            return 0
        if args.command == "generate":
            if args.output.exists():
                raise ValueError("output already exists; choose a new --output path")
            suite = generate_suite(seed=args.seed, count=args.count)
            write_json(args.output, suite.to_dict())
            print("Created {} validated development tasks in {} (seed {})".format(
                len(suite.tasks), args.output, args.seed))
            return 0
        if args.command == "validate":
            suite = load_suite(args.tasks) if args.tasks is not None else DEFAULT_SUITE
            validation = validate_suite(suite)
            if args.export is not None:
                write_json(args.export, validation.to_dict())
            killed = sum(bool(check.killed_by) for check in validation.mutations)
            print("{}: {} tasks; {}/{} mutants caught; {}".format(
                suite.id, len(suite.tasks), killed, len(validation.mutations),
                "accepted" if validation.accepted else "rejected"))
            for error in validation.errors:
                print(error)
            for check in validation.mutations:
                if not check.killed_by:
                    print("surviving mutant: " + check.mutant_id)
            return 0 if validation.accepted else 1
        if args.command in ("run", "run-tool"):
            # Fail before any paid request when an output artifact already exists.
            if args.output.exists():
                raise ValueError("output already exists; choose a new --output path")
            config = load_config(args.config)
            if isinstance(config, LocalConfig) and (args.command != "run" or not args.native_async):
                raise ValueError("local provider requires forge run --native-async")
            if args.command == "run-tool":
                result = evaluate_tool(config, make_provider(config, allow_network=args.allow_network))
            else:
                options = None
                native_options = None
                admission_options = None
                admission_values = {name: getattr(args, name) for name in admission_flags if getattr(args, name) is not None}
                native_flags = (args.max_pending_retries, args.request_timeout, args.retry_seed, args.max_retry_after)
                if args.native_async:
                    if args.coordinate or admission_values:
                        admission_options = AdmissionOptions(**admission_values)
                    native_options = NativeRuntimeOptions(
                        concurrency=args.concurrency if args.concurrency is not None else 1,
                        max_attempts=args.max_attempts, retry_delay_seconds=args.retry_delay,
                        **{key: value for key, value in zip(
                            ("max_pending_retries", "request_timeout_seconds", "retry_seed", "max_retry_after_seconds"),
                            native_flags) if value is not None})
                elif any(value is not None for value in native_flags) or args.coordinate or admission_values:
                    raise ValueError("native runtime options require --native-async")
                elif args.concurrency is not None:
                    options = RuntimeOptions(args.concurrency, args.max_attempts, args.retry_delay)
                elif args.max_attempts != 1 or args.retry_delay != 0.25:
                    raise ValueError("retry options require --concurrency")
                suite = load_suite(args.tasks) if args.tasks is not None else DEFAULT_SUITE
                validate_suite(suite).require_valid()
                if native_options is not None:
                    result = asyncio.run(run_native(config, suite, native_options, args.allow_network, admission_options))
                else:
                    provider = make_provider(config, allow_network=args.allow_network)
                    result = (evaluate(config, provider, suite) if options is None else
                              asyncio.run(evaluate_async(config, provider, suite, options)))
            write_json(args.output, result)
            summary = result["summary"]
            print("{passed}/{total} passed; {errors} provider errors. Saved {path}".format(
                **summary, path=args.output))
            return 0 if summary["failed"] == 0 else 1
        result = load_report(args.input)
        if args.export is not None:
            write_json(args.export, result)
        summary = result["summary"]
        print("{passed}/{total} passed ({pass_rate:.1%}); {failed} failed; {errors} provider errors".format(**summary))
        return 0
    except (OSError, ValueError) as exc:
        print("forge: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
