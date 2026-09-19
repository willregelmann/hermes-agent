"""``restart_peer_gateway`` — ask a registered peer agent's gateway to restart gracefully.

An agent cannot restart its own gateway (the restart kills the process doing it), so restarts used
to need a person. A peer's gateway can restart itself cleanly on request; this tool is how an agent
asks. No ssh, no turn on the peer. Holding the peer's API key is the consent.
See ``docs/capabilities/peer-gateway-restart.md``.

Runs in-process rather than via ``hermes peer restart`` in the terminal because sandboxed terminal
backends have neither the ``hermes`` binary nor the peer registry and keys.
"""

import json
import urllib.error

from tools.registry import registry, tool_error


def restart_peer_gateway(peer: str) -> str:
    from hermes_cli.subcommands.peer import PeerRestartUnsupported, request_peer_gateway_restart

    target = (peer or "").strip()
    if not target:
        return tool_error("restart_peer_gateway needs a peer: a name from your registered peers, "
                          "optionally <name>/<profile>.")
    try:
        result = request_peer_gateway_restart(target)
    except (ValueError, LookupError, PermissionError, PeerRestartUnsupported) as exc:
        return tool_error(str(exc))
    except urllib.error.HTTPError as exc:
        return tool_error(f"Peer rejected the restart request (HTTP {exc.code}).")
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
        return tool_error(f"Could not reach the peer's gateway: {exc}. If it is down it can't be "
                          "asked to restart; tell a person who can reach that machine.")
    return json.dumps({"success": True, **result}, ensure_ascii=False)


RESTART_PEER_GATEWAY_SCHEMA = {
    "name": "restart_peer_gateway",
    "description": (
        "Ask a registered peer agent's gateway to restart gracefully: its turns in progress "
        "finish first, then it restarts under its service manager. Every agent that gateway "
        "serves restarts. Returns once the peer has accepted the request; message the peer "
        "afterwards to confirm it came back."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "peer": {
                "type": "string",
                "description": "A registered peer name, or <name>/<profile> on a multiplexed peer.",
            },
        },
        "required": ["peer"],
    },
}


def check_peers_configured() -> bool:
    """Opt-in: shown only when this profile has registered peers (``bot_peers`` in config.yaml)."""
    from hermes_cli.subcommands.peer import _load_peers
    return bool(_load_peers())


registry.register(
    name="restart_peer_gateway", toolset="peers", schema=RESTART_PEER_GATEWAY_SCHEMA,
    check_fn=check_peers_configured,
    handler=lambda args, **kw: restart_peer_gateway(args.get("peer", "")),
    emoji="🔁")
