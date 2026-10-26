"""Trusted loopback-only chat fixture; no model or external services involved."""

import asyncio
import json

from forge.config import LocalConfig
from forge.providers import MockProvider


class LocalChatFixture:
    def __init__(self):
        self.requests = []
        self.headers = []
        self.runner = None

    async def __aenter__(self):
        from aiohttp import web

        async def chat(request):
            data = await request.json()
            self.requests.append(data)
            self.headers.append(dict(request.headers))
            answer = MockProvider().complete(data["messages"][0]["content"])
            if data.get("stream"):
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                midpoint = max(1, len(answer) // 2)
                events = [{"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}]
                events.extend({"model": data["model"], "choices": [{"index": 0, "delta": {"content": part},
                              "finish_reason": None}]} for part in (answer[:midpoint], answer[midpoint:]) if part)
                events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                events.append({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}})
                for event in events:
                    raw = ("data: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n").encode()
                    # Fragment UTF-8 and framing independently of event boundaries.
                    for start in range(0, len(raw), 7):
                        await response.write(raw[start:start + 7])
                    await asyncio.sleep(0)
                await response.write(b"data: [DONE]\n\n")
                await response.write_eof()
                return response
            return web.json_response({"model": data["model"],
                "choices": [{"message": {"content": answer}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", chat)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *args):
        await self.runner.cleanup()

    def config(self, **overrides):
        fields = {"model": "fixture-text-model", "base_url": "http://127.0.0.1:{}/v1".format(self.port),
                  "model_revision": "fixture-revision-1", "tokenizer_revision": "fixture-tokenizer-1",
                  "model_license": "fixture-only-no-model", "dtype": "fixture", "server_version": "fixture-http-v1"}
        fields.update(overrides)
        return LocalConfig(**fields)
