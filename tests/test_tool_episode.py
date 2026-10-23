import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forge.cli import main
from forge.config import Config, write_json
from forge.evaluation import load_report
from forge.providers import MockProvider, OpenRouterProvider, ProviderError
from forge.tool_episode import MAX_RESPONSE_CHARS, evaluate_tool, validate_tool_report


class ScriptedProvider:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(json.loads(prompt))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


READ = '{"tool":"read_fixture","path":"inventory.json"}'
ANSWER = '{"answer":"7"}'


class ToolEpisodeTests(unittest.TestCase):
    def test_mock_reads_fixture_then_answers_reproducibly(self):
        first = evaluate_tool(Config(), MockProvider())
        second = evaluate_tool(Config(), MockProvider())
        self.assertEqual(first["results"][0]["trace"], second["results"][0]["trace"])
        self.assertEqual(first["summary"]["passed"], 1)
        trace = first["results"][0]["trace"]
        self.assertEqual([entry["event"] for entry in trace], ["tool_result", "answer"])
        initial = json.loads(trace[0]["prompt"])
        self.assertNotIn("expected", initial)
        self.assertNotIn("files", initial)
        self.assertEqual(initial["trace"], [])
        validate_tool_report(first)

    def test_answer_requires_successful_read_and_exact_value(self):
        for responses in ((ANSWER,), (READ, '{"answer":"8"}'), (READ, '{"answer":"7\\n"}')):
            with self.subTest(responses=responses):
                result = evaluate_tool(Config(), ScriptedProvider(*responses))
                self.assertFalse(result["results"][0]["passed"])
                self.assertEqual(result["summary"]["errors"], 0)
                validate_tool_report(result)

    def test_unknown_paths_are_data_errors_and_can_be_recovered(self):
        for path in ("../../.env", "/etc/passwd", "missing", ["inventory.json"]):
            provider = ScriptedProvider(json.dumps({"tool": "read_fixture", "path": path}), READ, ANSWER)
            with self.subTest(path=path), patch("builtins.open", side_effect=AssertionError("host file read")):
                report = evaluate_tool(Config(), provider)
                self.assertTrue(report["results"][0]["passed"])
                self.assertEqual(report["results"][0]["trace"][0]["event"], "tool_error")
                validate_tool_report(report)

    def test_invalid_actions_stop_without_executing_code(self):
        responses = ("incorrect", "[]", "null", '{"tool":"shell","path":"echo unsafe"}',
                     '{"answer":7}', '{"answer":"7","extra":true}', "x" * (MAX_RESPONSE_CHARS + 1))
        for response in responses:
            with self.subTest(response=response[:50]):
                report = evaluate_tool(Config(), ScriptedProvider(response))
                self.assertEqual(report["results"][0]["termination"], "invalid_action")
                self.assertFalse(report["results"][0]["passed"])
                validate_tool_report(report)
                if len(response) > MAX_RESPONSE_CHARS:
                    self.assertIsNone(report["results"][0]["trace"][0]["response"])

    def test_budget_is_enforced(self):
        provider = ScriptedProvider(READ, READ, READ, ANSWER)
        report = evaluate_tool(Config(), provider)
        self.assertEqual(len(provider.prompts), 3)
        self.assertEqual(report["results"][0]["termination"], "budget_exhausted")
        self.assertEqual(report["summary"]["failed"], 1)
        validate_tool_report(report)

    def test_provider_errors_are_sanitized_and_programming_errors_propagate(self):
        for response in (ProviderError("secret-key"), None):
            report = evaluate_tool(Config(), ScriptedProvider(response))
            self.assertNotIn("secret-key", json.dumps(report))
            self.assertEqual(report["summary"]["errors"], 1)
            validate_tool_report(report)
        with self.assertRaises(RuntimeError):
            evaluate_tool(Config(), ScriptedProvider(RuntimeError("bug")))

    def test_report_replay_rejects_inconsistent_evidence(self):
        original = evaluate_tool(Config(), MockProvider())
        mutations = (
            lambda r: r["task"]["files"].update({"inventory.json": "changed"}),
            lambda r: r.update(task_sha256="0" * 64),
            lambda r: r["results"][0].update(answer="8"),
            lambda r: r["results"][0].update(passed=1),
            lambda r: r["results"][0]["trace"][0].update(value="changed"),
            lambda r: r["results"][0]["trace"][1].update(prompt="changed"),
            lambda r: r["results"][0]["trace"].pop(),
            lambda r: r["results"][0]["trace"].append(r["results"][0]["trace"][0]),
            lambda r: r["summary"].update(passed=0),
        )
        for mutate in mutations:
            report = copy.deepcopy(original)
            mutate(report)
            with self.assertRaises(ValueError):
                validate_tool_report(report)

    def test_cli_run_report_export_and_overwrite_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, output, export = root / "config.json", root / "report.json", root / "export.json"
            write_json(config, Config().to_dict())
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                args = ["run-tool", "--config", str(config), "--output", str(output)]
                self.assertEqual(main(args), 0)
                self.assertEqual(main(["report", "--input", str(output), "--json", str(export)]), 0)
                self.assertEqual(load_report(output), load_report(export))
                with patch("forge.cli.make_provider") as factory:
                    self.assertEqual(main(args), 2)
                    factory.assert_not_called()
                faulty = Config(mock_mode="faulty")
                other_config, failed = root / "faulty.json", root / "failed.json"
                write_json(other_config, faulty.to_dict())
                self.assertEqual(main(["run-tool", "--config", str(other_config), "--output", str(failed)]), 1)
                self.assertEqual(load_report(failed)["summary"]["failed"], 1)

    def test_cli_network_remains_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            config, output = Path(directory) / "config.json", Path(directory) / "report.json"
            write_json(config, Config(provider="openrouter", model="test/model").to_dict())
            with patch("urllib.request.urlopen") as transport, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["run-tool", "--config", str(config), "--output", str(output)]), 2)
                transport.assert_not_called()
            self.assertFalse(output.exists())

    def test_openrouter_episode_uses_fake_transport_and_sends_tool_history(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch("urllib.request.urlopen") as transport:
            response = transport.return_value.__enter__.return_value
            response.read.side_effect = [
                json.dumps({"choices": [{"message": {"content": content}}]}).encode()
                for content in (READ, ANSWER)
            ]
            report = evaluate_tool(Config(provider="openrouter", model="test/model"),
                                   OpenRouterProvider("test/model", allow_network=True))
            self.assertEqual(report["summary"]["passed"], 1)
            self.assertEqual(transport.call_count, 2)
            request = transport.call_args.args[0]
            prompt = json.loads(json.loads(request.data)["messages"][0]["content"])
            self.assertEqual(prompt["trace"][0]["event"], "tool_result")
            self.assertNotIn("test-key", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
