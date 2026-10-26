"""Trusted loopback-only chat fixture; no model or external services involved."""

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
