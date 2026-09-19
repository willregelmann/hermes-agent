"""``POST /api/gateway/restart``: a key holder asks this gateway to restart gracefully.

The caller is typically a registered peer agent (``restart_peer_gateway`` /
``hermes peer restart``). An agent cannot restart its own gateway from a command, since the
restart kills the process running it; the gateway can restart itself, which is what ``/restart``
does. This route exposes that same path over the API. Holding ``API_SERVER_KEY`` is the consent.
See ``docs/capabilities/peer-gateway-restart.md``.

The restart is process-wide: a ``/p/<profile>/`` request restarts every profile this gateway serves.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

RESTART_PATH = "/api/gateway/restart"


def _http_routes(adapter: Any) -> list[tuple[str, str, Any]]:
    async def _handler(request):
        return await handle_gateway_restart(adapter, request)

    return [("POST", RESTART_PATH, _handler)]


def _runner(adapter: Any) -> Any:
    runner = getattr(adapter, "gateway_runner", None)
    if runner is None:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
    return runner


async def handle_gateway_restart(adapter: Any, request: Any) -> Any:
    from aiohttp import web

    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err
    runner = _runner(adapter)
    if runner is None or not hasattr(runner, "request_restart"):
        return web.json_response({"error": "no gateway runner in this process"}, status=503)
    # Same guard /restart uses: a restart already under way is not requested twice.
    if getattr(runner, "_restart_requested", False) or getattr(runner, "_draining", False):
        return web.json_response({"restarting": True, "already": True}, status=202)
    draining = runner._running_agent_count()
    # Under a service manager or container, exit 75 lets the supervisor restart us; otherwise the
    # gateway re-launches itself detached. Identical to the /restart slash command.
    from gateway.restart import is_container_restart_context, is_gateway_supervisor_process
    via_service = is_gateway_supervisor_process() or is_container_restart_context()
    logger.info("Gateway restart requested over the API (draining %d active turn(s)): %s",
                draining, adapter._request_audit_log_suffix(request))
    runner.request_restart(detached=not via_service, via_service=via_service)
    return web.json_response({"restarting": True, "draining": draining}, status=202)
