import asyncio
import json
import unittest
from email.utils import formatdate
from unittest.mock import patch

try:
    from forge.async_http import AsyncOpenRouterProvider, parse_retry_after
except ImportError:
    AsyncOpenRouterProvider = None

from forge.providers import ProviderError, ProviderTimeoutError, RetryableProviderError


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
