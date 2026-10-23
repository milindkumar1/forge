"""The init -> run -> report command-line path."""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from forge.config import Config, load_config, write_json
from forge.evaluation import evaluate, load_report
from forge.providers import make_provider


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="forge", description="Run small verifiable development evaluations.")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create an offline sample config")
    init.add_argument("--config", type=Path, default=Path("forge.json"))
    run = commands.add_parser("run", help="evaluate the fixed development suite")
    run.add_argument("--config", type=Path, default=Path("forge.json"))
    run.add_argument("--output", type=Path, default=Path("forge-results.json"))
    run.add_argument("--allow-network", action="store_true", help="allow paid OpenRouter inference")
    report = commands.add_parser("report", help="summarize saved results and optionally export JSON")
    report.add_argument("--input", type=Path, default=Path("forge-results.json"))
    report.add_argument("--json", type=Path, dest="export")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            write_json(args.config, Config().to_dict())
            print("Created " + str(args.config))
            return 0
        if args.command == "run":
            # Fail before any paid request when an output artifact already exists.
            if args.output.exists():
                raise ValueError("output already exists; choose a new --output path")
            config = load_config(args.config)
            result = evaluate(config, make_provider(config, allow_network=args.allow_network))
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
