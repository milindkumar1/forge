import asyncio
import contextlib
import importlib.util
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from forge.cli import main, run_native
from forge.config import Config, LocalConfig, load_config, validate_local_config, write_json
from forge.evaluation import load_report
from forge.native_runtime import NativeRuntimeOptions
from forge.providers import make_provider
from forge.suites import DEFAULT_SUITE
from local_http_fixture import LocalChatFixture

if importlib.util.find_spec("aiohttp") is not None:
    from forge.async_http import AsyncLocalProvider
else:
    AsyncLocalProvider = None


def config(**changes):
    fields = {"model": "fixture-model", "base_url": "http://127.0.0.1:8000/v1",
              "model_revision": "fixture-revision", "tokenizer_revision": "fixture-tokenizer",
              "model_license": "fixture-only", "dtype": "fixture", "server_version": "fixture-v1"}
    fields.update(changes)
    return LocalConfig(**fields)


class LocalConfigTests(unittest.TestCase):
    def test_versioned_roundtrip_preserves_legacy_config(self):
        with tempfile.TemporaryDirectory() as directory:
            for index, value in enumerate((Config(), config())):
                path = Path(directory) / str(index)
                write_json(path, value.to_dict())
                self.assertEqual(load_config(path), value)
            bad = config().to_dict()
            bad["api_key"] = "not-allowed"
            path = Path(directory) / "bad.json"
            write_json(path, bad)
            with self.assertRaises(ValueError):
                load_config(path)

    def test_local_urls_are_loopback_only_without_credentials_or_routing_overrides(self):
        for base in ("http://127.0.0.1:8000/v1", "https://127.0.0.1/v1/", "http://[::1]:8000/v1"):
            validate_local_config(config(base_url=base))
        for base in ("https://example.com/v1", "http://localhost:8000/v1", "http://0.0.0.0/v1",
                     "http://127.0.0.1.evil/v1", "http://secret@127.0.0.1/v1", "ftp://127.0.0.1/v1",
                     "http://127.0.0.1/v1?secret=x", "http://127.0.0.1/v1?", "http://127.0.0.1/v1#",
                     "http://127.0.0.1/v2", "http://127.0.0.1:99999/v1", "http://127.0.0.1/\nv1", None):
            with self.subTest(base=base), self.assertRaises(ValueError):
                validate_local_config(config(base_url=base))

    def test_invalid_provenance_and_generation_fields_are_rejected(self):
        for changes in ({"model_revision": ""}, {"tokenizer_revision": None}, {"model_license": " "},
                        {"dtype": 7}, {"server_version": "x" * 513}, {"max_tokens": True},
                        {"max_tokens": 0}, {"max_tokens": 32769}, {"stream": "true"}, {"schema_version": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_local_config(config(**changes))

    def test_local_cannot_accidentally_use_synchronous_or_openrouter_path(self):
        with patch("urllib.request.urlopen") as transport:
            with self.assertRaisesRegex(ValueError, "native async"):
                make_provider(config(), allow_network=True)
            transport.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.json"
            write_json(path, config().to_dict())
            for command in ("run", "run-tool"):
                with contextlib.redirect_stderr(io.StringIO()), patch("forge.cli.make_provider") as factory:
                    self.assertEqual(main([command, "--config", str(path), "--output", str(Path(directory) / "out")]), 2)
                    factory.assert_not_called()


@unittest.skipIf(AsyncLocalProvider is None, "install the http extra")
class LocalTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_eval_preserves_usage_provenance_and_safe_request_contract(self):
        async with LocalChatFixture() as fixture:
            local = fixture.config(max_tokens=16)
            with patch.dict("os.environ", {"OPENROUTER_API_KEY": "must-not-send"}):
                report = await run_native(local, DEFAULT_SUITE, NativeRuntimeOptions(3), True)
            self.assertEqual(report["summary"]["passed"], 6)
            self.assertEqual(report["config"], local.to_dict())
            self.assertEqual(report["runtime"]["charged_tokens"], 42)
            self.assertTrue(all(a["reported_tokens"] == 7 for r in report["results"] for a in r["attempts"]))
            self.assertTrue(all(request["max_tokens"] == 16 for request in fixture.requests))
            self.assertTrue(all("Authorization" not in headers for headers in fixture.headers))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report.json"
                write_json(path, report)
                self.assertEqual(load_report(path), report)

    async def test_network_is_gated_before_session_creation(self):
        with self.assertRaisesRegex(ValueError, "allow-network"):
            AsyncLocalProvider(config())
        with patch("forge.async_http.aiohttp.ClientSession") as session:
            with self.assertRaisesRegex(ValueError, "loopback"):
                AsyncLocalProvider(config(base_url="http://example.com/v1"), allow_network=True)
            session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
