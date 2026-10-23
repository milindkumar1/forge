import contextlib
import copy
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from forge.cli import main
from forge.config import Config, write_json
from forge.evaluation import evaluate, load_report
from forge.providers import MockProvider
from forge.suites import DEFAULT_SUITE, MAX_SUITE_BYTES, load_suite, parse_suite, validate_suite
from forge.tasks import Task


class SuiteTests(unittest.TestCase):
    def test_builtin_suite_has_reference_and_mutation_evidence(self):
        validation = validate_suite(DEFAULT_SUITE)
        self.assertTrue(validation.accepted)
        self.assertEqual(len(validation.mutations), 6)
        self.assertTrue(all(check.killed_by for check in validation.mutations))
        evidence = validation.to_dict()
        self.assertEqual(evidence["suite_sha256"], DEFAULT_SUITE.digest)
        self.assertEqual(evidence["validator_version"], "text-validator-v1")

    def test_roundtrip_and_format_independent_digest(self):
        data = DEFAULT_SUITE.to_dict()
        self.assertEqual(parse_suite(data), DEFAULT_SUITE)
        reversed_keys = dict(reversed(list(data.items())))
        self.assertEqual(parse_suite(json.loads(json.dumps(reversed_keys, indent=4))).digest, DEFAULT_SUITE.digest)

    def test_digest_changes_when_content_provenance_or_order_changes(self):
        original = DEFAULT_SUITE.digest
        variants = [replace(DEFAULT_SUITE, id="another-id"),
                    replace(DEFAULT_SUITE, source="manual:another-source"),
                    replace(DEFAULT_SUITE, tasks=tuple(reversed(DEFAULT_SUITE.tasks))),
                    replace(DEFAULT_SUITE, tasks=(replace(DEFAULT_SUITE.tasks[0], text="other"),) + DEFAULT_SUITE.tasks[1:])]
        for variant in variants:
            with self.subTest(variant=variant.id):
                self.assertNotEqual(variant.digest, original)

    def test_suite_schema_rejects_invalid_metadata_and_collections(self):
        variants = [{"schema_version": True}, {"schema_version": 2}, {"id": ""},
                    {"id": "bad\nidentifier"}, {"source": " "}, {"source": 7},
                    {"split": "training"}, {"split": "held-out"},
                    {"tasks": []}, {"tasks": {}}, {"tasks": [None]},
                    {"tasks": [DEFAULT_SUITE.to_dict()["tasks"][0]] * 1001},
                    {"executable": "not allowed"}]
        for variant in variants:
            with self.subTest(fields=list(variant)):
                data = DEFAULT_SUITE.to_dict()
                data.update(variant)
                with self.assertRaises(ValueError):
                    parse_suite(data)

    def test_task_schema_rejects_duplicates_unknown_operations_and_bad_types(self):
        variants = [{"task_id": ""}, {"task_id": "space in id"}, {"operation": "execute_python"},
                    {"operation": []}, {"text": None}, {"text": "x" * 10001},
                    {"expected": 1}, {"known_wrong": False}, {"code": "not allowed"}]
        for variant in variants:
            with self.subTest(fields=list(variant)):
                data = DEFAULT_SUITE.to_dict()
                data["tasks"][0].update(variant)
                with self.assertRaises(ValueError):
                    parse_suite(data)
        data = DEFAULT_SUITE.to_dict()
        data["tasks"].append(copy.deepcopy(data["tasks"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_suite(data)

    def test_reference_check_rejects_a_wrong_label_even_when_verifier_accepts_it(self):
        task = replace(DEFAULT_SUITE.tasks[0], expected="forge", known_wrong="egrof")
        self.assertTrue(task.verify("forge"))
        suite = replace(DEFAULT_SUITE, tasks=(task,))
        validation = validate_suite(suite)
        self.assertFalse(validation.accepted)
        self.assertIn("expected answer disagrees", validation.errors[0])
        self.assertTrue(any("known_wrong is actually correct" in error for error in validation.errors))
        self.assertEqual(validation.mutations, ())

    def test_correct_but_weak_cases_fail_mutation_coverage(self):
        suite = replace(DEFAULT_SUITE, tasks=(Task("palindrome", "reverse", "aba", "aba", "bad"),))
        validation = validate_suite(suite)
        self.assertEqual(validation.errors, ())
        self.assertFalse(validation.accepted)
        survivors = [check.mutant_id for check in validation.mutations if not check.killed_by]
        self.assertEqual(survivors, ["reverse.identity"])
        with self.assertRaisesRegex(ValueError, "surviving mutant: reverse.identity"):
            validation.require_valid()

    def test_numeric_suite_requires_cases_for_sort_order_and_duplicates(self):
        suite = replace(DEFAULT_SUITE, tasks=(Task("single", "sort_numbers", "5", "5", "bad"),))
        validation = validate_suite(suite)
        self.assertFalse(validation.accepted)
        self.assertEqual(len([check for check in validation.mutations if not check.killed_by]), 2)

    def test_only_operations_in_suite_require_mutation_coverage(self):
        suite = replace(DEFAULT_SUITE, tasks=(Task("custom", "reverse", "abc", "cba", "abc"),))
        validation = validate_suite(suite)
        self.assertTrue(validation.accepted)
        self.assertEqual(len(validation.mutations), 2)

    def test_numeric_inputs_are_checked_before_mutants_execute(self):
        for text in ("", "1_000,2", "1.5,2", "1,", "1, 2", "9" * 19, "NaN"):
            with self.subTest(text=text):
                suite = replace(DEFAULT_SUITE, tasks=(Task("bad-numeric", "sort_numbers", text, "bad", "bad"),))
                validation = validate_suite(suite)
                self.assertFalse(validation.accepted)
                self.assertIn("requires comma-separated", validation.errors[0])
                self.assertEqual(validation.mutations, ())

    def test_references_handle_unicode_whitespace_and_canonical_numbers(self):
        cases = (
            Task("unicode-reverse", "reverse", "a\u0301\U0001f408", "\U0001f408\u0301a", "wrong"),
            Task("unicode-whitespace", "word_count", "\u2003one\u00a0two\n", "2", "wrong"),
            Task("canonical-numbers", "sort_numbers", "+10,002,-0,2,-3", "-3,0,2,2,10", "wrong"),
        )
        suite = replace(DEFAULT_SUITE, tasks=cases)
        self.assertTrue(validate_suite(suite).accepted)
        self.assertEqual(evaluate(Config(), MockProvider(), suite)["summary"]["passed"], 3)

    def test_evaluator_refuses_invalid_suites_before_calling_provider(self):
        provider = Mock()
        suite = replace(DEFAULT_SUITE, tasks=(Task("weak", "reverse", "aba", "aba", "bad"),))
        with self.assertRaisesRegex(ValueError, "suite validation failed"):
            evaluate(Config(), provider, suite)
        provider.complete.assert_not_called()


class SuiteFileAndCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tasks = self.root / "tasks.json"
        self.config = self.root / "config.json"
        self.output = self.root / "results.json"
        write_json(self.tasks, DEFAULT_SUITE.to_dict())
        write_json(self.config, Config().to_dict())

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_checked_in_example_matches_builtin_suite(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "text-dev-v1.json"
        self.assertEqual(load_suite(example), DEFAULT_SUITE)

    def test_validate_without_config_or_provider_and_export_evidence(self):
        evidence = self.root / "validation.json"
        with patch("forge.cli.make_provider") as factory:
            code, stdout, stderr = self.invoke("validate", "--tasks", str(self.tasks), "--json", str(evidence))
            self.assertEqual(code, 0, stderr)
            self.assertIn("6/6 mutants caught; accepted", stdout)
            factory.assert_not_called()
        self.assertTrue(json.loads(evidence.read_text())["accepted"])
        self.assertEqual(self.invoke("validate")[0], 0)
        original = evidence.read_bytes()
        self.assertEqual(self.invoke("validate", "--json", str(evidence))[0], 2)
        self.assertEqual(evidence.read_bytes(), original)

    def test_rejected_validation_still_exports_diagnostics(self):
        suite = replace(DEFAULT_SUITE, tasks=(Task("weak", "reverse", "aba", "aba", "bad"),))
        self.tasks.write_text(json.dumps(suite.to_dict()))
        evidence = self.root / "rejected.json"
        code, stdout, _ = self.invoke("validate", "--tasks", str(self.tasks), "--json", str(evidence))
        self.assertEqual(code, 1)
        self.assertIn("surviving mutant: reverse.identity", stdout)
        self.assertFalse(json.loads(evidence.read_text())["accepted"])

    def test_custom_suite_run_and_report_survive_source_file_changes(self):
        suite = replace(DEFAULT_SUITE, id="custom-reversals", source="manual:test-case",
                        tasks=(Task("custom-reverse", "reverse", "abcd", "dcba", "abcd"),))
        self.tasks.write_text(json.dumps(suite.to_dict()))
        code, _, stderr = self.invoke("run", "--config", str(self.config), "--tasks", str(self.tasks), "--output", str(self.output))
        self.assertEqual(code, 0, stderr)
        self.tasks.write_text("the original file changed")
        result = load_report(self.output)
        self.assertEqual(result["summary"]["total"], 1)
        self.assertEqual(result["results"][0]["answer"], "dcba")
        self.assertEqual(result["suite"]["sha256"], suite.digest)
        self.assertEqual(result["suite"]["source"], "manual:test-case")
        self.assertTrue(result["validation"]["accepted"])
        self.assertEqual(result["suite"]["tasks"], suite.to_dict()["tasks"])
        self.assertEqual(self.invoke("report", "--input", str(self.output))[0], 0)

    def test_invalid_suite_prevents_provider_creation_and_output(self):
        suite = replace(DEFAULT_SUITE, tasks=(Task("wrong", "reverse", "abc", "abc", "bad"),))
        self.tasks.write_text(json.dumps(suite.to_dict()))
        write_json(self.root / "network.json", replace(Config(), provider="openrouter", model="test/model").to_dict())
        with patch("forge.cli.make_provider") as factory, patch("urllib.request.urlopen") as transport:
            code, _, stderr = self.invoke("run", "--config", str(self.root / "network.json"), "--tasks", str(self.tasks),
                                          "--allow-network", "--output", str(self.output))
            self.assertEqual(code, 2)
            self.assertIn("suite validation failed", stderr)
            factory.assert_not_called()
            transport.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_malformed_missing_and_oversize_files_return_input_errors(self):
        self.assertEqual(self.invoke("validate", "--tasks", str(self.root / "missing.json"))[0], 2)
        for content in (b"{", b"[]", b"\xff", b" " * (MAX_SUITE_BYTES + 1)):
            with self.subTest(prefix=content[:5]):
                self.tasks.write_bytes(content)
                self.assertEqual(self.invoke("validate", "--tasks", str(self.tasks))[0], 2)

    def test_report_detects_changed_snapshot_answer_or_result_count(self):
        original = evaluate(Config(), MockProvider())
        for mutation in ("snapshot", "answer", "count", "validation"):
            with self.subTest(mutation=mutation):
                report = copy.deepcopy(original)
                if mutation == "snapshot":
                    report["suite"]["tasks"][0]["text"] = "tampered"
                elif mutation == "answer":
                    report["results"][0]["answer"] = "wrong answer with passed still true"
                elif mutation == "validation":
                    report["validation"]["suite_sha256"] = "different-digest"
                else:
                    report["suite"]["tasks"].pop()
                    suite_data = {key: value for key, value in report["suite"].items()
                                  if key not in ("sha256", "digest_version")}
                    report["suite"]["sha256"] = parse_suite(suite_data).digest
                self.output.write_text(json.dumps(report))
                with self.assertRaises(ValueError):
                    load_report(self.output)

    def test_legacy_schema_one_reports_remain_readable(self):
        report = evaluate(Config(), MockProvider())
        report["suite"] = {"id": "text-dev-v1", "split": "development", "sha256": "old-digest"}
        del report["validation"]
        self.output.write_text(json.dumps(report))
        self.assertEqual(load_report(self.output)["summary"]["passed"], 6)


if __name__ == "__main__":
    unittest.main()
