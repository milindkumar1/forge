"""The init -> run -> report command-line path."""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from forge.config import Config, load_config, write_json
from forge.evaluation import evaluate, load_report
from forge.generation import generate_suite
from forge.providers import make_provider
from forge.suites import DEFAULT_SUITE, load_suite, validate_suite


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
    run.add_argument("--allow-network", action="store_true", help="allow paid OpenRouter inference")
    run.add_argument("--tasks", type=Path, help="load a versioned JSON suite instead of built-in tasks")
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
        if args.command == "run":
            # Fail before any paid request when an output artifact already exists.
            if args.output.exists():
                raise ValueError("output already exists; choose a new --output path")
            config = load_config(args.config)
            suite = load_suite(args.tasks) if args.tasks is not None else DEFAULT_SUITE
            validate_suite(suite).require_valid()
            result = evaluate(config, make_provider(config, allow_network=args.allow_network), suite)
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
