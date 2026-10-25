import asyncio
import json
import importlib.util
import unittest
from email.utils import formatdate
from unittest.mock import patch
from dataclasses import replace

if importlib.util.find_spec("aiohttp") is not None:
    from forge.async_http import AsyncOpenRouterProvider, parse_retry_after
else:
    AsyncOpenRouterProvider = None

from forge.providers import ProviderError, ProviderTimeoutError, RetryableProviderError
from forge.config import Config
from forge.native_runtime import NativeRuntimeOptions, evaluate_native
from forge.suites import DEFAULT_SUITE


class Response:
    def __init__(self, status=200, headers=None, chunks=None, gate=None):
        self.status = status
        self.headers = headers or {}
        self.chunks = chunks if chunks is not None else [b'{"choices":[{"message":{"content":"ok"}}]}']
        self.gate = gate
        self.content = self
        self.entered = asyncio.Event()
        self.closed = False

    async def __aenter__(self):
        self.entered.set()
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk
        if self.gate is not None:
            await self.gate.wait()


@unittest.skipIf(AsyncOpenRouterProvider is None, "install the http extra")
class AsyncHttpTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, timeout=30):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fake-secret"}):
            return AsyncOpenRouterProvider("test/model", allow_network=True,
                                           request_timeout_seconds=timeout)

    async def test_pool_lifecycle_and_request_contract(self):
        response = Response()
        async with self.provider() as provider:
            session = provider._session
            with patch.object(session, "post", return_value=response) as post:
                self.assertEqual(await provider.acomplete("test"), "ok")
            self.assertFalse(post.call_args.kwargs["allow_redirects"])
            self.assertEqual(post.call_args.kwargs["json"]["messages"], [{"role": "user", "content": "test"}])
        self.assertTrue(session.closed)
        self.assertTrue(response.closed)

    async def test_status_and_body_validation_sanitize_errors(self):
        for response, expected in ((Response(429, {"Retry-After": "7"}), RetryableProviderError),
                                   (Response(503), RetryableProviderError),
                                   (Response(401), ProviderError), (Response(302), ProviderError),
                                   (Response(chunks=[b"secret"]), ProviderError),
                                   (Response(chunks=[b"x" * 1_000_001]), ProviderError),
                                   (Response(chunks=[b'{"choices":[]}']), ProviderError)):
            async with self.provider() as provider:
                with patch.object(provider._session, "post", return_value=response):
                    with self.assertRaises(expected) as caught:
                        await provider.acomplete("test")
                self.assertNotIn("secret", str(caught.exception))
                self.assertTrue(response.closed)
                if response.status == 429:
                    self.assertEqual(caught.exception.retry_after_seconds, 7)

    async def test_deadline_covers_body_stream_and_releases_response(self):
        response = Response(gate=asyncio.Event())
        async with self.provider(timeout=0.01) as provider:
            with patch.object(provider._session, "post", return_value=response):
                with self.assertRaises(ProviderTimeoutError):
                    await provider.acomplete("test")
        self.assertTrue(response.closed)

    async def test_deadline_covers_connection_and_header_wait(self):
        cancelled = asyncio.Event()

        class WaitingHeaders(Response):
            async def __aenter__(self):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        async with self.provider(timeout=0.01) as provider:
            with patch.object(provider._session, "post", return_value=WaitingHeaders()):
                with self.assertRaises(ProviderTimeoutError):
                    await provider.acomplete("test")
        self.assertTrue(cancelled.is_set())

    async def test_external_cancellation_propagates_and_closes_response(self):
        response = Response(gate=asyncio.Event())
        async with self.provider() as provider:
            with patch.object(provider._session, "post", return_value=response):
                task = asyncio.create_task(provider.acomplete("test"))
                await response.entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(response.closed)

    async def test_real_loopback_http_retries_and_body_deadline_close_sockets(self):
        requests = []
        disconnected = asyncio.Event()
        handlers = set()
        stalled = False

        async def handle(reader, writer):
            current = asyncio.current_task()
            handlers.add(current)
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                              if line.lower().startswith(b"content-length:"))
                requests.append(json.loads(await reader.readexactly(length)))
                if stalled:
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n{")
                    await writer.drain()
                    await reader.read()
                    disconnected.set()
                else:
                    status = b"429 Too Many Requests" if len(requests) == 1 else b"200 OK"
                    body = b'{"choices":[{"message":{"content":"egrof"}}]}'
                    writer.write(b"HTTP/1.1 " + status + b"\r\nRetry-After: 0\r\nConnection: close\r\nContent-Length: "
                                 + str(len(body)).encode() + b"\r\n\r\n" + body)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(current)

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        address = "http://127.0.0.1:{}/".format(server.sockets[0].getsockname()[1])
        try:
            with patch("forge.async_http.ENDPOINT", address):
                async with self.provider() as provider:
                    report = await evaluate_native(Config(), provider,
                        replace(DEFAULT_SUITE, tasks=(DEFAULT_SUITE.tasks[0],)),
                        NativeRuntimeOptions(max_attempts=2, retry_delay_seconds=0))
                self.assertEqual(report["summary"]["passed"], 1)
                self.assertEqual(report["runtime"]["retries"], 1)
                self.assertEqual(len(requests), 2)
                stalled = True
                async with self.provider(timeout=0.01) as provider:
                    with self.assertRaises(ProviderTimeoutError):
                        await provider.acomplete("stall")
                await asyncio.wait_for(disconnected.wait(), 2)
        finally:
            server.close()
            await server.wait_closed()
            for task in list(handlers):
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

    def test_retry_after_seconds_dates_invalid_and_huge_values(self):
        now = 1_700_000_000
        for header, expected in (("10", 10), (" 0 ", 0), (formatdate(now + 17, usegmt=True), 17),
                                 (formatdate(now - 10, usegmt=True), 0), (None, 0), ("bad", 0),
                                 ("-1", 0), ("1.5", 0), ("inf", 0), ("9" * 1000, 1e12)):
            with self.subTest(header=header):
                self.assertEqual(parse_retry_after(header, now=now), expected)

    def test_network_and_options_are_gated_before_session_creation(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "allow-network"):
                AsyncOpenRouterProvider("test/model")
            with self.assertRaisesRegex(ValueError, "OPENROUTER_API_KEY"):
                AsyncOpenRouterProvider("test/model", allow_network=True)
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fake"}):
            for fields in ({"request_timeout_seconds": 0}, {"request_timeout_seconds": float("nan")},
                           {"max_connections": True}, {"max_connections": 65}):
                with self.assertRaises(ValueError):
                    AsyncOpenRouterProvider("test/model", allow_network=True, **fields)


if __name__ == "__main__":
    unittest.main()
