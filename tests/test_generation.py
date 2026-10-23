import contextlib
import io
import json
import platform
import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from forge import __version__
from forge.cli import main
from forge.evaluation import load_report
from forge.generation import GENERATOR_ID, _candidate, generate_suite
from forge.suites import MAX_SUITE_BYTES, load_suite, parse_suite, validate_suite
from forge.tasks import Task


class GenerationTests(unittest.TestCase):
    def test_same_recipe_reproduces_entire_suite_and_digest(self):
        first = generate_suite(seed=42, count=12)
        second = generate_suite(seed=42, count=12)
        self.assertEqual(first, second)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(parse_suite(first.to_dict()), first)

    def test_seed_changes_actual_inputs_not_only_provenance(self):
        first = generate_suite(seed=1)
        second = generate_suite(seed=2)
        self.assertNotEqual([(task.operation, task.text) for task in first.tasks],
                            [(task.operation, task.text) for task in second.tasks])
        self.assertNotEqual(first.digest, second.digest)

    def test_provenance_records_generator_version_seed_count_and_runtime(self):
        suite = generate_suite(seed=42, count=7)
        source = urlsplit(suite.source)
        self.assertEqual(source.scheme, "generator")
        self.assertEqual(source.path, "forge/" + GENERATOR_ID)
        self.assertEqual(parse_qs(source.query), {
            "seed": ["42"], "count": ["7"], "python": [platform.python_version()],
            "runtime": [platform.python_implementation()],
        })
        self.assertEqual(suite.split, "development")

    def test_generated_suites_pass_all_six_mutants_across_seeds_and_count_bounds(self):
        for seed in (0, 1, 42, 2**32 - 1):
            for count in (3, 6, 12, 1000):
                with self.subTest(seed=seed, count=count):
                    suite = generate_suite(seed=seed, count=count)
                    self.assertEqual(len(suite.tasks), count)
                    self.assertEqual(len({task.task_id for task in suite.tasks}), count)
                    self.assertEqual(len({(task.operation, task.text) for task in suite.tasks}), count)
                    validation = validate_suite(suite)
                    self.assertTrue(validation.accepted)
                    self.assertEqual(len(validation.mutations), 6)
                    payload = json.dumps(suite.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")
                    self.assertLessEqual(len(payload) + 1, MAX_SUITE_BYTES)

    def test_empty_and_singleton_boundaries_are_included_once(self):
        suite = generate_suite(count=30)
        self.assertEqual(sum(task.operation == "reverse" and task.text == "" for task in suite.tasks), 1)
        self.assertEqual(sum(task.operation == "word_count" and task.text == "" for task in suite.tasks), 1)
        self.assertEqual(sum(task.operation == "sort_numbers" and "," not in task.text for task in suite.tasks), 1)

    def test_generation_does_not_consume_global_random_state(self):
        state = random.getstate()
        generate_suite(seed=42)
        self.assertEqual(random.getstate(), state)

    def test_invalid_types_and_ranges_are_rejected(self):
        for seed in (-1, 2**32, True, 1.0, "1"):
            with self.subTest(seed=seed), self.assertRaisesRegex(ValueError, "seed"):
                generate_suite(seed=seed)
        for count in (0, 2, 1001, False, 3.0, "12"):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "count"):
                generate_suite(count=count)

    def test_duplicate_sampling_exhaustion_fails_instead_of_looping_forever(self):
        repeated = Task("repeated", "reverse", "ab", "ba", "ab")
        with patch("forge.generation._candidate", return_value=repeated) as candidate:
            with patch("forge.generation.MAX_ATTEMPTS_PER_TASK", 2):
                with self.assertRaisesRegex(ValueError, "unique task within 2 attempts"):
                    generate_suite(count=3)
            self.assertEqual(candidate.call_count, 3)


class GenerationCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tasks = self.root / "generated.json"

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_generate_needs_no_config_provider_or_network(self):
        with patch("forge.cli.make_provider") as factory, patch("urllib.request.urlopen") as transport:
            code, stdout, stderr = self.invoke("generate", "--seed", "42", "--output", str(self.tasks))
            self.assertEqual(code, 0, stderr)
            self.assertIn("12 validated development tasks", stdout)
            factory.assert_not_called()
            transport.assert_not_called()
        self.assertEqual(load_suite(self.tasks), generate_suite(seed=42))

    def test_repeated_generation_is_byte_identical_and_never_overwrites(self):
        for name in ("first.json", "second.json"):
            self.assertEqual(self.invoke("generate", "--seed", "42", "--output", str(self.root / name))[0], 0)
        first = self.root / "first.json"
        before = first.read_bytes()
        self.assertEqual(before, (self.root / "second.json").read_bytes())
        with patch("forge.cli.generate_suite") as generator:
            self.assertEqual(self.invoke("generate", "--output", str(first))[0], 2)
            generator.assert_not_called()
        self.assertEqual(first.read_bytes(), before)

    def test_invalid_arguments_and_missing_parent_create_no_task_file(self):
        for args in (("--count", "2"), ("--count", "1001"), ("--seed", "-1"), ("--seed", str(2**32))):
            with self.subTest(args=args):
                code, _, _ = self.invoke("generate", *args, "--output", str(self.tasks))
                self.assertEqual(code, 2)
                self.assertFalse(self.tasks.exists())
        self.assertEqual(self.invoke("generate", "--output", str(self.root / "missing" / "tasks.json"))[0], 2)

    def test_corrupt_generated_label_fails_validation_before_any_write(self):
        def wrong_candidate(rng, index):
            return replace(_candidate(rng, index), expected="incorrect reference")

        with patch("forge.generation._candidate", side_effect=wrong_candidate):
            code, _, stderr = self.invoke("generate", "--output", str(self.tasks))
            self.assertEqual(code, 2)
            self.assertIn("expected answer disagrees with trusted reference", stderr)
        self.assertFalse(self.tasks.exists())

    def test_generated_suite_flows_through_validation_run_and_report(self):
        config = self.root / "config.json"
        results = self.root / "results.json"
        validation = self.root / "validation.json"
        exported = self.root / "export.json"
        commands = (
            ("generate", "--seed", "7", "--count", "30", "--output", str(self.tasks)),
            ("validate", "--tasks", str(self.tasks), "--json", str(validation)),
            ("init", "--config", str(config)),
            ("run", "--config", str(config), "--tasks", str(self.tasks), "--output", str(results)),
            ("report", "--input", str(results), "--json", str(exported)),
        )
        for args in commands:
            with self.subTest(command=args[0]):
                code, _, stderr = self.invoke(*args)
                self.assertEqual(code, 0, stderr)
        suite = load_suite(self.tasks)
        report = load_report(exported)
        self.assertEqual(report["summary"]["passed"], 30)
        self.assertEqual(report["suite"]["source"], suite.source)
        self.assertEqual(report["suite"]["sha256"], suite.digest)
        self.assertEqual(json.loads(validation.read_text()), report["validation"])
        self.assertEqual(report["forge_version"], __version__)


if __name__ == "__main__":
    unittest.main()
