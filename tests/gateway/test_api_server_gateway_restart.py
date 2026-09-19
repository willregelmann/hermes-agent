"""POST /api/gateway/restart: a key holder triggers the same graceful restart as ``/restart``.

Driven through the adapter's real route table, so the route that ships is the route under test.
See docs/capabilities/peer-gateway-restart.md (#54).
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

KEY = "sk-peer-restart-test-key-0123456789abcdef"


class _Runner:
    """The runner surface the route touches; request_restart flips state like the real one."""

    def __init__(self, active: int = 2):
        self.calls = []
        self._restart_requested = False
        self._draining = False
        self._active = active

    def _running_agent_count(self) -> int:
        return self._active

    def request_restart(self, *, detached: bool = False, via_service: bool = False) -> bool:
        self.calls.append({"detached": detached, "via_service": via_service})
        self._restart_requested = True
        self._draining = True
        return True


def _client(runner):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": KEY}))
    adapter.gateway_runner = runner
    app = web.Application()
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
    return TestClient(TestServer(app))


@pytest.mark.asyncio
async def test_authorised_request_restarts_once_and_repeat_does_not_restart_again():
    runner = _Runner(active=2)
    async with _client(runner) as client:
        first = await client.post("/api/gateway/restart", headers={"Authorization": f"Bearer {KEY}"})
        assert first.status == 202
        assert await first.json() == {"restarting": True, "draining": 2}
        assert len(runner.calls) == 1
        # A restart already under way is reported, never requested a second time.
        again = await client.post("/api/gateway/restart", headers={"Authorization": f"Bearer {KEY}"})
        assert again.status == 202
        assert (await again.json()).get("already") is True
        assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_wrong_or_missing_key_never_restarts():
    runner = _Runner()
    async with _client(runner) as client:
        wrong = await client.post("/api/gateway/restart", headers={"Authorization": "Bearer nope"})
        missing = await client.post("/api/gateway/restart")
        assert (wrong.status, missing.status) == (401, 401)
    assert runner.calls == []
