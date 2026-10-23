import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from forge.cli import main
from forge.config import Config, load_config, write_json
from forge.evaluation import evaluate, load_report
from forge.providers import MockProvider, OpenRouterProvider, ProviderError, make_provider
from forge.tasks import TASKS, suite_digest, validate_fixtures


class FixtureTests(unittest.TestCase):
    def test_mock_solves_every_fixture_without_reading_expected_answers(self):
        provider = MockProvider()
        for task in TASKS:
            with self.subTest(task=task.task_id):
                self.assertEqual(provider.complete(task.prompt), task.expected)
                self.assertNotIn("expected", json.loads(task.prompt))

    def test_verifiers_reject_known_mutations_and_extra_whitespace(self):
        validate_fixtures()
        for task in TASKS:
            with self.subTest(task=task.task_id):
                self.assertTrue(task.verify(task.expected))
                self.assertFalse(task.verify(task.known_wrong))
                self.assertFalse(task.verify(task.expected + "\n"))
        self.assertEqual(len(suite_digest()), 64)

    def test_suite_catches_three_independent_mutated_implementations(self):
        mutants = {
            "reverse": lambda text: text,  # Forgot to reverse.
            "word_count": lambda text: str(len(text.split(" "))),  # Mishandles whitespace.
            "sort_numbers": lambda text: ",".join(sorted(text.split(","))),  # Lexical sort.
        }
        for operation, mutant in mutants.items():
            with self.subTest(operation=operation):
                cases = [task for task in TASKS if task.operation == operation]
                self.assertTrue(any(not task.verify(mutant(task.text)) for task in cases))


class EvaluationTests(unittest.TestCase):
    def test_repeatable_outputs_and_provenance(self):
        first = evaluate(Config(), MockProvider())
        second = evaluate(Config(), MockProvider())
        self.assertEqual(first["suite"], second["suite"])
        self.assertEqual(first["summary"], {"total": 6, "passed": 6, "failed": 0, "errors": 0, "pass_rate": 1.0})
        self.assertEqual(first["config"], Config().to_dict())
        for left, right in zip(first["results"], second["results"]):
            for field in ("task_id", "prompt", "expected", "answer", "passed", "error"):
                self.assertEqual(left[field], right[field])

    def test_wrong_answers_are_failures(self):
        result = evaluate(replace(Config(), mock_mode="faulty"), MockProvider("faulty"))
        self.assertEqual(result["summary"]["failed"], 6)
        self.assertEqual(result["summary"]["errors"], 0)

    def test_provider_errors_are_recorded_without_secret_details(self):
        class BrokenProvider:
            def complete(self, prompt: str) -> str:
                raise ProviderError("secret-token-in-exception")

        result = evaluate(Config(), BrokenProvider())
        self.assertEqual(result["summary"]["errors"], 6)
        self.assertEqual(result["summary"]["passed"], 0)
        self.assertNotIn("secret-token", json.dumps(result))


class ProviderTests(unittest.TestCase):
    def test_openrouter_requires_both_network_consent_and_key(self):
        with patch.dict(os.environ, {}, clear=True), patch("urllib.request.urlopen") as transport:
            with self.assertRaisesRegex(ValueError, "allow-network"):
                OpenRouterProvider("test/model")
            with self.assertRaisesRegex(ValueError, "OPENROUTER_API_KEY"):
                OpenRouterProvider("test/model", allow_network=True)
            transport.assert_not_called()

    def test_openrouter_request_uses_fake_transport(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch("urllib.request.urlopen") as transport:
            transport.return_value.__enter__.return_value.read.return_value = json.dumps({
                "choices": [{"message": {"content": "egrof"}}]
            }).encode()
            provider = make_provider(replace(Config(), provider="openrouter", model="test/model"), allow_network=True)
            self.assertEqual(provider.complete("reverse forge"), "egrof")
            request = transport.call_args.args[0]
            self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/chat/completions")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
            payload = json.loads(request.data)
            self.assertEqual(payload["model"], "test/model")
            self.assertEqual(payload["messages"], [{"role": "user", "content": "reverse forge"}])
            self.assertEqual(transport.call_args.kwargs["timeout"], 30)

    def test_transport_and_invalid_response_errors_are_sanitized(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            provider = OpenRouterProvider("test/model", allow_network=True)
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("secret")):
            with self.assertRaises(ProviderError) as caught:
                provider.complete("prompt")
            self.assertNotIn("secret", str(caught.exception))
        for response in (b"not json", b"{}", b'{"choices": []}', b'{"choices": [{"message": {"content": null}}]}'):
            with self.subTest(response=response), patch("urllib.request.urlopen") as transport:
                transport.return_value.__enter__.return_value.read.return_value = response
                with self.assertRaises(ProviderError):
                    provider.complete("prompt")


class FileAndCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.output = self.root / "results.json"

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_init_run_report_and_export(self):
        self.assertEqual(self.invoke("init", "--config", str(self.config))[0], 0)
        self.assertEqual(self.invoke("run", "--config", str(self.config), "--output", str(self.output))[0], 0)
        exported = self.root / "export.json"
        code, stdout, stderr = self.invoke("report", "--input", str(self.output), "--json", str(exported))
        self.assertEqual(code, 0, stderr)
        self.assertIn("6/6 passed", stdout)
        self.assertEqual(json.loads(exported.read_text()), load_report(self.output))

    def test_no_clobber_for_config_results_and_export(self):
        self.invoke("init", "--config", str(self.config))
        config_before = self.config.read_bytes()
        self.assertEqual(self.invoke("init", "--config", str(self.config))[0], 2)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.invoke("run", "--config", str(self.config), "--output", str(self.output))
        report_before = self.output.read_bytes()
        with patch("forge.cli.make_provider") as factory:
            self.assertEqual(self.invoke("run", "--config", str(self.config), "--output", str(self.output))[0], 2)
            factory.assert_not_called()
        self.assertEqual(self.invoke("report", "--input", str(self.output), "--json", str(self.output))[0], 2)
        self.assertEqual(self.output.read_bytes(), report_before)

    def test_failed_run_exit_code_and_saved_evidence(self):
        write_json(self.config, replace(Config(), mock_mode="faulty").to_dict())
        self.assertEqual(self.invoke("run", "--config", str(self.config), "--output", str(self.output))[0], 1)
        self.assertEqual(load_report(self.output)["summary"]["failed"], 6)

    def test_network_is_opt_in_at_cli_boundary(self):
        write_json(self.config, replace(Config(), provider="openrouter", model="test/model").to_dict())
        with patch("urllib.request.urlopen") as transport:
            code, _, stderr = self.invoke("run", "--config", str(self.config), "--output", str(self.output))
            self.assertEqual(code, 2)
            self.assertIn("allow-network", stderr)
            transport.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_missing_and_malformed_files_produce_cli_errors(self):
        self.assertEqual(self.invoke("run", "--config", str(self.config))[0], 2)
        for payload in ("{", "[]", '{"schema_version": 99}'):
            with self.subTest(payload=payload):
                self.config.write_text(payload)
                self.assertEqual(self.invoke("run", "--config", str(self.config))[0], 2)
                self.assertEqual(self.invoke("report", "--input", str(self.config))[0], 2)

    def test_config_rejects_invalid_types_unknown_fields_and_models(self):
        variants = [dict(schema_version=True), dict(provider="other"), dict(model=""),
                    dict(model="false-label"), dict(mock_mode="other"), dict(api_key="secret")]
        for variant in variants:
            with self.subTest(variant=variant):
                data = Config().to_dict()
                data.update(variant)
                self.config.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    load_config(self.config)

    def test_report_rejects_inconsistent_summary_and_status(self):
        for mutation in ("summary", "duplicate", "status", "empty", "error"):
            with self.subTest(mutation=mutation):
                result = evaluate(Config(), MockProvider())
                if mutation == "summary":
                    result["summary"]["passed"] = 0
                elif mutation == "duplicate":
                    result["results"][1]["task_id"] = result["results"][0]["task_id"]
                elif mutation == "status":
                    result["results"][0]["passed"] = 1
                elif mutation == "empty":
                    result["results"] = []
                else:
                    result["results"][0]["error"] = "error despite pass"
                self.output.write_text(json.dumps(result))
                self.assertEqual(self.invoke("report", "--input", str(self.output))[0], 2)

    def test_module_entrypoint_in_subprocess(self):
        result = subprocess.run([sys.executable, "-m", "forge.cli", "init", "--config", str(self.config)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(load_config(self.config), Config())


if __name__ == "__main__":
    unittest.main()
