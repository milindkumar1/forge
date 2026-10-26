import asyncio
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forge.cli import run_native
from forge.config import write_json
from forge.evaluation import load_report
from forge.inference import InferenceInfo, StreamTrace, parse_inference_info, token_usage
from forge.native_runtime import NativeRuntimeOptions
from forge.providers import ProviderError, ProviderTimeoutError, RetryableProviderError
from forge.streaming import read_chat_stream, sse_data
from forge.suites import DEFAULT_SUITE
from local_http_fixture import LocalChatFixture
from test_async_http import Response
from test_local_provider import config

if importlib.util.find_spec("aiohttp") is not None:
    from forge.async_http import AsyncLocalProvider
else:
    AsyncLocalProvider = None


async def chunks(parts):
    for part in parts:
        yield part


def event(value):
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


def delta(text=None, reason=None):
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": reason}]}


class StreamParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_utf8_fragmentation_bom_comments_and_line_endings(self):
        raw = "\ufeff: comment\r\ndata: 猫\rdata: second\r\nevent: message\r\n\r\ndata: [DONE]\n\n".encode()
        values = [value async for value in sse_data(chunks([bytes([value]) for value in raw]))]
        self.assertEqual(values, ["猫\nsecond", "[DONE]"])

    async def test_usage_and_content_arrivals_are_distinct_from_tokens(self):
        clock = iter((1.5, 2.5, 3.0))
        raw = (event({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
               + event(delta("ab")) + event(delta("猫")) + event(delta(reason="stop"))
               + event({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})
               + b"data: [DONE]\n\n")
        result = await read_chat_stream(chunks([raw]), model="fixture", now=lambda: next(clock), started=1.0)
        self.assertEqual(result.text, "ab猫")
        self.assertEqual(result.total_tokens, 8)
        self.assertEqual(result.inference.completion_tokens, 3)
        self.assertEqual(result.inference.stream.content_arrival_seconds, (0.5, 1.5))
        self.assertEqual(result.inference.stream.duration_seconds, 2)
        self.assertEqual(parse_inference_info(result.inference.to_dict(), 8), result.inference)

    async def test_empty_answer_and_length_termination_are_recorded(self):
        for text, reason in ((None, "stop"), ("partial", "length")):
            raw = event(delta(text, reason)) + b"data: [DONE]\n\n"
            result = await read_chat_stream(chunks([raw]), model="fixture", now=lambda: 2, started=1)
            self.assertEqual(result.text, text or "")
            self.assertEqual(result.inference.stream.finish_reason, reason)
            self.assertEqual(len(result.inference.stream.content_arrival_seconds), int(bool(text)))

    async def test_eof_and_invalid_payloads_never_return_partial_success(self):
        for raw, expected in ((event(delta("partial")), RetryableProviderError),
                              (b"data: {", RetryableProviderError),
                              (b"data: [DONE]\n\n", ProviderError),
                              (b"data: not-json\n\n", ValueError),
                              (event({"error": "secret"}), ProviderError),
                              (event({"choices": [{"index": True, "delta": {}}]}), ProviderError),
                              (event({"choices": [{"index": 0, "delta": {"tool_calls": []}}]}), ProviderError),
                              (event(delta(7)), ProviderError),
                              (event(dict(delta("x"), model="wrong")), ProviderError),
                              (event(delta("x", "tool_calls")), ProviderError),
                              (event(delta("x", "stop")) + event(delta("late")), ProviderError)):
            with self.subTest(raw=raw[:30]), self.assertRaises(expected):
                await read_chat_stream(chunks([raw]), model="fixture", now=lambda: 2, started=1)

    async def test_bounds_apply_to_wire_lines_events_and_delta_count(self):
        with patch("forge.streaming.MAX_STREAM_BYTES", 10), self.assertRaises(ProviderError):
            _ = [value async for value in sse_data(chunks([b":" * 11]))]
        with patch("forge.streaming.MAX_EVENT_CHARS", 12), self.assertRaises(ProviderError):
            _ = [value async for value in sse_data(chunks([b"data:123456\ndata:123456\n\n"]))]
        with patch("forge.streaming.MAX_CONTENT_DELTAS", 1), self.assertRaises(ProviderError):
            await read_chat_stream(chunks([event(delta("a")) + event(delta("b"))]),
                                   model="fixture", now=lambda: 2, started=1)
        with self.assertRaises(UnicodeDecodeError):
            _ = [value async for value in sse_data(chunks([b"data: \xff\n\n"]))]


class InferenceEvidenceTests(unittest.TestCase):
    def test_inconsistent_usage_and_timing_are_rejected(self):
        for usage in ({"total_tokens": True}, {"completion_tokens": -1},
                      {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 4}):
            with self.assertRaises(ValueError):
                token_usage({"usage": usage})
        self.assertEqual(token_usage({"usage": {"prompt_tokens": 2, "completion_tokens": 3}}), (5, 2, 3))
        for times, elapsed in (((1, 0.5), 2), ((-1,), 2), ((True,), 2), ((3,), 2), ((), float("nan"))):
            with self.assertRaises(ValueError):
                StreamTrace(times, elapsed, "stop")


@unittest.skipIf(AsyncLocalProvider is None, "install the http extra")
class StreamTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_streaming_eval_usage_and_evidence_roundtrip(self):
        async with LocalChatFixture() as fixture:
            local = fixture.config(stream=True)
            report = await run_native(local, DEFAULT_SUITE, NativeRuntimeOptions(3), True)
            self.assertEqual(report["summary"]["passed"], 6)
            self.assertEqual(report["runtime"]["charged_tokens"], 42)
            self.assertTrue(all(request["stream_options"] == {"include_usage": True} for request in fixture.requests))
            self.assertTrue(all(request["seed"] == 0 for request in fixture.requests))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "stream.json"
                write_json(path, report)
                self.assertEqual(load_report(path), report)
                for field, value in (("content_arrival_seconds", [-1]), ("timing_unit", "token"),
                                     ("duration_seconds", 1000)):
                    tampered = copy.deepcopy(report)
                    tampered["results"][0]["attempts"][0]["inference"]["stream"][field] = value
                    path.write_text(json.dumps(tampered))
                    with self.assertRaises(ValueError):
                        load_report(path)

    async def test_stream_deadline_and_cancellation_close_partial_responses(self):
        raw = event(delta("partial"))
        for cancel in (False, True):
            response = Response(headers={"Content-Type": "text/event-stream"}, chunks=[raw], gate=asyncio.Event())
            async with AsyncLocalProvider(config(stream=True), allow_network=True,
                                           request_timeout_seconds=30 if cancel else 0.01) as provider:
                with patch.object(provider._session, "post", return_value=response):
                    if cancel:
                        task = asyncio.create_task(provider.acomplete_with_usage("test"))
                        await response.entered.wait()
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        with self.assertRaises(ProviderTimeoutError):
                            await provider.acomplete_with_usage("test")
            self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
