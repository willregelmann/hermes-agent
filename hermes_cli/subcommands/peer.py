"""``hermes peer`` — bot-to-bot DMs across machines/gateways.

A *peer* is another Hermes gateway running the ``api_server`` platform; its stock
API is the transport (no new server surface). ``dm`` resolves the remote canonical
"Bot Chat" session (creating it when missing) and runs ONE synchronous turn — the
cross-machine twin of ``hermes -p <bot> chat --in ~ -c "Bot Chat"``. ``run``/``status``
/``stop`` do the same turn through the async Runs API. Peer labels/URLs live in
config.yaml (``bot_peers``); the key lives in ``~/.hermes/.env`` as
``HERMES_PEER_<NAME>_KEY``. ``<peer>/<profile>`` targets the ``/p/<profile>/`` mirror.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BOT_CHAT_TITLE = "Bot Chat"
_PEER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PROFILE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# One synchronous agent turn can legitimately take minutes.
DM_TIMEOUT_S = 600
# Accept-only sends wait for the peer to ENQUEUE the turn, not to finish it.
# This is a genuine transport timeout: exceeding it means the peer did not
# answer the socket, which is a different fact from "the peer is thinking".
# Deliberately matched to LIST_TIMEOUT_S — both measure reachability, not work.
ACCEPT_TIMEOUT_S = 30
LIST_TIMEOUT_S = 30


def _self_origin() -> "dict | None":
    """Where a reply to THIS agent should be delivered, or None if unknowable.

    Returns ``{"agent": <name>, "host": <hostname>}`` read from
    ``identity.json``. Returns None rather than guessing: a fabricated return
    address is worse than none, because the peer would deliver a real reply to
    a place nobody reads and the send would still look successful.

    Deliberately NOT a session id. Which session a reply belongs in is the
    receiving gateway's business — it already resolves a peer's long-lived Bot
    Chat session by title. Sending a session id would also let one box name a
    session on another box, which is the shape of thing that should stay
    impossible.
    """
    import json as _json
    import os as _os
    import socket as _socket

    try:
        from hermes_constants import get_hermes_home

        path = _os.path.join(str(get_hermes_home()), "identity.json")
        with open(path, encoding="utf-8") as fh:
            data = _json.load(fh)
    except Exception:
        return None

    agent = data.get("agent")
    if isinstance(agent, dict):
        agent = agent.get("name")
    agent = (str(agent or "")).strip()
    if not agent:
        # No declared identity: say nothing rather than invent one.
        return None

    host = (str(data.get("host") or "")).strip()
    if not host:
        try:
            host = _socket.gethostname()
        except Exception:
            host = ""
    origin = {"agent": agent}
    if host:
        origin["host"] = host
    return origin


def _peer_key_env(name: str) -> str:
    return f"HERMES_PEER_{name.upper().replace('-', '_')}_KEY"


def _load_peers() -> dict:
    from hermes_cli.config import load_config

    cfg = load_config() or {}
    peers = cfg.get("bot_peers")
    return peers if isinstance(peers, dict) else {}


def _save_peers(peers: dict) -> None:
    from hermes_cli.config import load_config, save_config

    cfg = load_config() or {}
    cfg["bot_peers"] = peers
    save_config(cfg)


def _peer_secret(name: str) -> str:
    """The peer's API key: profile-scoped secret store first, raw env fallback."""
    env_name = _peer_key_env(name)
    try:
        from agent.secret_scope import get_secret

        return (get_secret(env_name, "") or "").strip()
    except Exception:
        import os

        return (os.environ.get(env_name) or "").strip()


def _request(
    url: str, key: str, *, method: str = "GET", body: dict | None = None,
    timeout: int = LIST_TIMEOUT_S, headers: dict[str, str] | None = None) -> dict:
    from hermes_cli.urllib_security import open_credentialed_url
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": "hermes-peer-dm"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    # The peer URL is user-registered (``hermes peer add``); a redirect to a
    # different origin must not carry the Authorization: Bearer key with it —
    # a compromised/MITM'd peer could otherwise harvest it. open_credentialed_url
    # strips non-safelisted headers across a cross-origin redirect.
    with open_credentialed_url(req, timeout=timeout) as resp:
        payload = resp.read().decode("utf-8", "replace")
    try:
        parsed = json.loads(payload)
    except ValueError as exc:
        raise RuntimeError(f"Peer returned non-JSON response: {payload[:200]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Peer returned a non-object JSON response")
    return parsed


def _base_url(peer: dict, profile: str | None) -> str:
    url = str(peer.get("url") or "").rstrip("/")
    if profile:
        # Multiplex mirror: same handlers, scoped to the named profile.
        return f"{url}/p/{urllib.parse.quote(profile, safe='')}"
    return url


def _find_bot_chat(base: str, key: str) -> str | None:
    """The remote canonical Bot Chat's session id, or None.

    Bot Mode always HIDES canonical chats, so the plain listing (which
    excludes hidden sessions) misses an existing Bot Chat and the caller
    would try to create a duplicate that the peer's UNIQUE(title) guard
    rejects (issue #91583). Newer peers support an exact-title lookup with
    ``include_hidden=1``; older peers ignore the unknown query params and
    return the ordinary visible listing, so this single request degrades
    to exactly the previous behavior against them.
    """
    query = urllib.parse.urlencode({"limit": 200, "title": BOT_CHAT_TITLE, "include_hidden": 1})
    listing = _request(f"{base}/api/sessions?{query}", key)
    for session in listing.get("data") or []:
        if isinstance(session, dict) and (session.get("title") or "").strip() == BOT_CHAT_TITLE:
            return str(session.get("id") or "") or None
    return None


def _ensure_bot_chat(base: str, key: str) -> str:
    existing = _find_bot_chat(base, key)
    if existing:
        return existing
    try:
        created = _request(
            f"{base}/api/sessions", key, method="POST",
            body={"title": BOT_CHAT_TITLE, "source": "bot_peer_dm"})
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        if exc.code == 400 and "title" in detail.lower():
            # Older peer (no title/include_hidden lookup support): its
            # canonical Bot Chat exists but is hidden, so we couldn't see it
            # and the create collided with the UNIQUE(title) guard.
            raise RuntimeError(
                f"Peer already has a '{BOT_CHAT_TITLE}' session but it is hidden and the "
                f"peer's gateway is too old to expose hidden sessions to this lookup "
                f"(HTTP 400: {detail}). Update the peer's hermes-agent, or unhide the "
                f"session there: PATCH /api/sessions/<id> {{\"hidden\": false}}.") from exc
        raise
    # Real api_server wraps the row: {"object": "hermes.session", "session": {...}}.
    session = created.get("session") if isinstance(created.get("session"), dict) else created
    session_id = str(session.get("id") or session.get("session_id") or "")
    if not session_id:
        raise RuntimeError("Peer did not return a session id for the new Bot Chat")
    return session_id


def _parse_target(target: str) -> tuple[str, str | None]:
    """``<peer>`` or ``<peer>/<profile>`` → (peer, profile|None)."""
    raw = (target or "").strip()
    peer, _, profile = raw.partition("/")
    peer = peer.strip()
    profile = profile.strip() or None
    if not peer:
        raise ValueError("Peer name required (hermes peer dm <peer>[/<agent>] ...)")
    if profile and not _PROFILE_RE.match(profile):
        raise ValueError(f"Invalid agent/profile name: {profile!r}")
    return peer, profile


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", "replace")
        parsed = json.loads(body)
        message = parsed.get("error", {}).get("message") if isinstance(parsed, dict) else None
        return message or body[:200]
    except Exception:
        return str(exc)


def _resolve_peer_target(target: str) -> tuple[str, str | None, dict, str]:
    """Resolve a registered target to ``(name, profile, config, key)``."""
    peer_name, profile = _parse_target(target)
    peer = _load_peers().get(peer_name)
    if not isinstance(peer, dict) or not peer.get("url"):
        raise LookupError(f"No peer named '{peer_name}'. Run: hermes peer list")
    key = _peer_secret(peer_name)
    if not key:
        raise PermissionError(
            f"No API key for peer '{peer_name}'. Set it: hermes peer add {peer_name} "
            f"--url <url> --key <key> (or add {_peer_key_env(peer_name)}=<key> to ~/.hermes/.env)")
    return peer_name, profile, peer, key


def _message_from_args(args) -> str:
    message = (getattr(args, "message", None) or "").strip()
    if not message and not sys.stdin.isatty():
        message = sys.stdin.read().strip()
    return message


def _peer_run_durability(base: str, key: str) -> bool | None:
    """Return durable support, or None when an older peer cannot advertise it."""
    try:
        capabilities = _request(f"{base}/v1/capabilities", key)
    except Exception:
        return None
    features = capabilities.get("features")
    if not isinstance(features, dict):
        return None
    contract = features.get("runs_idempotency")
    if not isinstance(contract, dict) or not contract.get("supported"):
        return None
    return bool(contract.get("durable"))


def _peer_failure(peer_name: str, exc: Exception) -> int:
    """Print a peer HTTP rejection or transport failure to stderr; always exit 1."""
    if isinstance(exc, urllib.error.HTTPError):
        detail = _http_error_detail(exc)
        print(f"Peer '{peer_name}' rejected the request (HTTP {exc.code}): {detail}",
              file=sys.stderr)
    else:
        print(f"Could not reach peer '{peer_name}': {exc}", file=sys.stderr)
    return 1


def _emit(args, payload: dict, text_lines: list[str]) -> int:
    if getattr(args, "json", False):
        print(json.dumps(payload))
    else:
        for line in text_lines:
            print(line)
    return 0


def _peer_add(args) -> int:
    name = (args.name or "").strip().lower()
    if not _PEER_NAME_RE.match(name):
        print(f"Invalid peer name: {name!r} (lowercase, digits, -, _; max 64)", file=sys.stderr)
        return 2
    url = (args.url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        print("Peer --url must be an http(s) gateway base URL, e.g. http://spark.lan:8377", file=sys.stderr)
        return 2
    peers = _load_peers()
    peers[name] = {"url": url.rstrip("/"), **({"note": args.note.strip()} if getattr(args, "note", "") else {})}
    _save_peers(peers)
    key = (getattr(args, "key", "") or "").strip()
    if key:
        from hermes_cli.config import save_env_value

        save_env_value(_peer_key_env(name), key)
        print(f"Peer '{name}' saved ({url}) — key stored as {_peer_key_env(name)} in ~/.hermes/.env")
    else:
        print(
            f"Peer '{name}' saved ({url}). No key given — set the peer's API_SERVER_KEY with:\n"
            f"  hermes peer add {name} --url {url} --key <key>\n"
            f"  (or add {_peer_key_env(name)}=<key> to ~/.hermes/.env)")
    return 0


def _peer_remove(args) -> int:
    name = (args.name or "").strip().lower()
    peers = _load_peers()
    if name not in peers:
        print(f"No peer named '{name}'.", file=sys.stderr)
        return 1
    peers.pop(name)
    _save_peers(peers)
    print(f"Peer '{name}' removed (its {_peer_key_env(name)} entry in .env is kept; delete it manually if unused).")
    return 0


def _peer_list(args) -> int:
    peers = _load_peers()
    if not peers:
        print("No peers registered. Add one: hermes peer add <name> --url http://host:port --key <API_SERVER_KEY>")
        return 0
    for name in sorted(peers):
        entry = peers[name] if isinstance(peers[name], dict) else {}
        has_key = "key set" if _peer_secret(name) else f"NO KEY ({_peer_key_env(name)} unset)"
        note = f" — {entry.get('note')}" if entry.get("note") else ""
        print(f"{name}\t{entry.get('url', '?')}\t[{has_key}]{note}")
    return 0


def _peer_run_ctl(args, action: str, peer_name: str, profile: str | None, base: str,
                  key: str) -> int:
    """``status`` / ``stop`` on an asynchronous run."""
    run_id = (getattr(args, "run_id", None) or "").strip()
    if not run_id:
        print("Run ID required.", file=sys.stderr)
        return 2
    stop = action == "stop"
    try:
        result = _request(
            f"{base}/v1/runs/{urllib.parse.quote(run_id, safe='')}" + ("/stop" if stop else ""),
            key, method="POST" if stop else "GET", body={} if stop else None)
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
        return _peer_failure(peer_name, exc)
    if getattr(args, "json", False):
        print(json.dumps({"peer": peer_name, "profile": profile, **result}))
        return 0
    print(f"{run_id}: {result.get('status', 'unknown')}")
    if not stop and result.get("output"):
        print(result["output"])
    elif not stop and result.get("error"):
        print(result["error"], file=sys.stderr)
    return 0


def _turn_body(message: str, *, message_key: str, **extra) -> dict:
    """Request body for one turn. ``author`` is added only when a dispatcher set HERMES_TURN_AUTHOR."""
    from agent.turn_author import turn_author_from_env

    body = {message_key: message, **extra}
    author = turn_author_from_env()
    if author is not None:
        body["author"] = author
    return body


def _peer_run(args, message: str, peer_name: str, profile: str | None, base: str, key: str) -> int:
    idempotency_key = (getattr(args, "idempotency_key", None) or f"peer-{uuid.uuid4().hex}").strip()
    if (not idempotency_key or len(idempotency_key) > 255
            or re.search(r"[\r\n\x00]", idempotency_key)):
        print("Idempotency key must be 1-255 characters without control newlines.", file=sys.stderr)
        return 2
    try:
        if _peer_run_durability(base, key) is not True:
            print(
                "Warning: this peer does not advertise restart-durable "
                "run replay; keep the run ID and avoid blind retries "
                "after a gateway restart.", file=sys.stderr)
        session_id = _ensure_bot_chat(base, key)
        result = _request(
            f"{base}/v1/runs", key, method="POST",
            body=_turn_body(message, message_key="input", session_id=session_id),
            headers={"Idempotency-Key": idempotency_key})
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
        return _peer_failure(peer_name, exc)
    run_id = str(result.get("run_id") or "")
    if not run_id:
        print(f"Peer '{peer_name}' did not return a run ID.", file=sys.stderr)
        return 1
    payload = {
        "peer": peer_name, "profile": profile, "session_id": session_id, "run_id": run_id,
        "status": result.get("status") or "started", "idempotency_key": idempotency_key,
        "replayed": bool(result.get("replayed", False))}
    replay = " (replayed)" if payload["replayed"] else ""
    return _emit(args, payload, [
        f"{run_id}: {payload['status']}{replay}", f"session_id: {session_id}",
        f"idempotency_key: {idempotency_key}"])


def _peer_dm(args, message: str, peer_name: str, profile: str | None, base: str, key: str) -> int:
    try:
        session_id = _ensure_bot_chat(base, key)
    except urllib.error.HTTPError as exc:
        return _peer_failure(peer_name, exc)
    except RuntimeError as exc:
        print(f"Peer '{peer_name}': {exc}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # No session id yet, so there is nothing to point a recovery at.
        print(f"Could not reach peer '{peer_name}' (resolving Bot Chat session): {exc}", file=sys.stderr)
        return 1

    # From here on the session id is known, so EVERY failure message names it. That is the
    # difference between a reply that is lost and one that is merely late: a completed turn is
    # persisted on the peer regardless of what happens to this socket.
    wait = not getattr(args, "no_wait", False)
    timeout = DM_TIMEOUT_S if wait else ACCEPT_TIMEOUT_S
    extra: dict = {}
    if not wait:
        # Ask the peer to enqueue the turn and answer immediately. Older peers ignore the flag and
        # run the turn synchronously, which is why the timeout below is still honoured.
        extra["wait"] = False
        # A RETURN ADDRESS: without it --no-wait delivers nowhere. The peer's completion event
        # carries the session the turn RAN in (on the peer's box); only the sender knows where the
        # answer belongs. A hint, not a command: older peers ignore unknown keys.
        origin = _self_origin()
        if origin:
            extra["reply_to"] = origin
            # Self-attestation so the receiver's record names a real sender; verification happens
            # against the receiver's OWN identity.json.
            extra["from"] = origin.get("agent")
    try:
        result = _request(
            f"{base}/api/sessions/{urllib.parse.quote(session_id, safe='')}/chat", key,
            method="POST", body=_turn_body(message, message_key="message", **extra), timeout=timeout)
    except urllib.error.HTTPError as exc:
        _peer_failure(peer_name, exc)
        print(f"session_id: {session_id}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"Peer '{peer_name}': {exc}", file=sys.stderr)
        print(f"session_id: {session_id}", file=sys.stderr)
        return 1
    except TimeoutError as exc:
        # DISTINCT from unreachable: the peer accepted the connection and is still working; the
        # socket gave up first. Reporting this as "could not reach peer" made completed work look
        # like a dead host.
        print(
            f"Peer '{peer_name}' did not answer within {timeout}s — the turn is "
            f"likely still running and its reply will persist in the session below. "
            f"This is NOT an unreachable peer: {exc}",
            file=sys.stderr,
        )
        print(f"session_id: {session_id}", file=sys.stderr)
        return 3
    except (urllib.error.URLError, OSError) as exc:
        print(f"Could not reach peer '{peer_name}': {exc}", file=sys.stderr)
        print(f"session_id: {session_id}", file=sys.stderr)
        return 1

    msg = result.get("message")
    reply = str(msg.get("content") or "") if isinstance(msg, dict) else ""
    effective_session_id = result.get("session_id") or session_id
    if not wait:
        # The response SHAPE decides whether wait:false meant anything: an accepted turn has no
        # assistant content yet, a completed one does. An older peer ran the whole turn
        # synchronously, and announcing "reply will arrive as an inbound DM" would discard it.
        if reply.strip():
            payload = {"peer": peer_name, "profile": profile, "session_id": effective_session_id,
                       "accepted": True, "honored_no_wait": False, "reply": reply}
            if getattr(args, "json", False):
                print(json.dumps(payload))
            else:
                print(reply)
                print(
                    f"Note: peer '{peer_name}' does not support --no-wait and ran "
                    f"the turn synchronously; the reply above is the whole answer "
                    f"and no inbound DM is coming.",
                    file=sys.stderr,
                )
            return 0
        payload = {"peer": peer_name, "profile": profile, "session_id": effective_session_id,
                   "accepted": True, "honored_no_wait": True,
                   "message_id": result.get("message_id") or result.get("id")}
        return _emit(args, payload, [f"accepted by '{peer_name}' — reply will arrive as an inbound DM",
                                     f"session_id: {effective_session_id}"])
    payload = {"peer": peer_name, "profile": profile, "session_id": effective_session_id, "reply": reply}
    return _emit(args, payload, [reply or "(no reply)"])


_REGISTRY_ACTIONS = {
    "add": _peer_add, "set": _peer_add, "remove": _peer_remove, "rm": _peer_remove,
    "list": _peer_list, "ls": _peer_list, None: _peer_list}


def cmd_peer(args) -> int:
    action = getattr(args, "peer_action", None)
    if action == "deploy":
        from hermes_cli.subcommands.peer_deploy import deploy

        return deploy(args.peer, args.sha, dry_run=bool(getattr(args, "dry_run", False)))
    if action in _REGISTRY_ACTIONS:
        return _REGISTRY_ACTIONS[action](args)
    if action not in {"dm", "run", "status", "stop"}:
        print("Unknown peer action. See: hermes peer --help", file=sys.stderr)
        return 2
    try:
        peer_name, profile, peer, key = _resolve_peer_target(args.target)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (LookupError, PermissionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    base = _base_url(peer, profile)
    if action in {"status", "stop"}:
        return _peer_run_ctl(args, action, peer_name, profile, base, key)
    message = _message_from_args(args)
    if not message:
        print("Message required (argument or stdin).", file=sys.stderr)
        return 2
    handler = _peer_run if action == "run" else _peer_dm
    return handler(args, message, peer_name, profile, base, key)


def build_peer_parser(subparsers) -> None:
    """Attach the ``peer`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "peer", help="Bot-to-bot DMs across machines (peer Hermes gateways)",
        description="Register other Hermes gateways as peers and message their agents. "
            "'hermes peer dm <peer>[/<agent>] \"...\"' delivers into the remote "
            "agent's canonical Bot Chat over the peer's API server and prints "
            "the reply — the cross-machine twin of 'hermes -p <bot> chat'. "
            "The peer must run the api_server platform; its API_SERVER_KEY is "
            "stored locally as a credential in ~/.hermes/.env.",
        epilog=(
            "Examples:\n"
            "  hermes peer add spark --url http://spark.lan:8377 --key <API_SERVER_KEY>\n"
            "  hermes peer list\n"
            '  hermes peer dm spark "Message from 🤖 dixie (@dixie): disk status?"\n'
            '  hermes peer dm spark/researcher "..."   # named profile on a multiplexed peer\n'
            "  hermes peer run spark --idempotency-key ticket-123 < long-task.txt\n"
            "  hermes peer status spark run_abc123\n"
            "  hermes peer stop spark run_abc123\n"
            "  hermes peer remove spark\n"
            "\n"
            "Exit codes: 0 ok, 1 delivery/peer error, 2 usage error."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    peer_sub = parser.add_subparsers(dest="peer_action")

    add_p = peer_sub.add_parser("add", aliases=["set"], help="Register (or update) a peer gateway")
    add_p.add_argument("name", help="Peer name (lowercase slug, e.g. spark, homelab)")
    add_p.add_argument("--url", required=True, help="Peer gateway base URL, e.g. http://spark.lan:8377")
    add_p.add_argument("--key", default="", help="The peer's API_SERVER_KEY (stored in ~/.hermes/.env)")
    add_p.add_argument("--note", default="", help="Optional description")

    peer_sub.add_parser("list", aliases=["ls"], help="List registered peers")

    rm_p = peer_sub.add_parser("remove", aliases=["rm"], help="Remove a peer")
    rm_p.add_argument("name", help="Peer name")

    def _remote(name: str, help: str, *, run_id: bool):
        sp = peer_sub.add_parser(name, help=help)
        sp.add_argument("target", help="<peer> or <peer>/<agent> (named profile on a multiplexed peer)")
        if run_id:
            sp.add_argument("run_id", help="Run ID returned by 'hermes peer run'")
        else:
            sp.add_argument("message", nargs="?", default=None, help="Message text (or stdin)")
            if name == "run":
                sp.add_argument(
                    "--idempotency-key", default=None, help="Stable retry key (generated when omitted)")
            if name == "dm":
                sp.add_argument(
                    "--no-wait", action="store_true", default=False, dest="no_wait",
                    help=(
                        "Return as soon as the peer ACCEPTS the message instead of blocking "
                        "for its whole turn. The reply arrives later as an inbound DM. Use "
                        "this for anything that makes the peer actually work — a long turn "
                        "over a blocking send is indistinguishable from an unreachable host."
                    ),
                )
        sp.add_argument("--json", action="store_true", default=False, help="Emit a JSON result")

    _remote("dm", "Message an agent on a peer gateway and print its reply", run_id=False)
    _remote("run", "Start a long peer turn asynchronously and return its run ID", run_id=False)
    _remote("status", "Read the status and final output of an asynchronous peer run", run_id=True)
    _remote("stop", "Stop one asynchronous peer run without affecting another turn", run_id=True)

    dep_p = peer_sub.add_parser(
        "deploy",
        help="Blue-green deploy a peer agent's harness to a SHA (never self)",
        description=(
            "Deploy another agent's harness and verify it came back healthy, "
            "rolling back automatically on failure or silence. Refuses to "
            "target this machine: the safety of the scheme depends on a second "
            "party being able to roll the first one back."
        ),
    )
    dep_p.add_argument("peer", help="Registered peer name (from bot_peers)")
    dep_p.add_argument("sha", help="Commit SHA to deploy (7-40 hex chars)")
    dep_p.add_argument("--dry-run", action="store_true", default=False,
                       help="Resolve, check targeting, and report without changing anything")

    parser.set_defaults(func=cmd_peer)
