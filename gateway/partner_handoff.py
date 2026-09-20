"""Tell-partner delivery: hand an intent from one of this agent's sessions to a partner's primary (#52).

* A person: the primary is whatever session is live in their chat (the routing index, so it follows
  compression and ``/new``). The intent arrives there as an internal event through the chat's
  adapter, exactly like a /loop tick: one user row (``internal_notification``), queued behind a
  running turn, and a bare ``[SILENT]`` reply is not delivered. That turn's reply goes to the person.
* Another agent: ``hermes peer`` sends it into ``Peer: <this agent>`` on the peer, without waiting;
  the peer's answer comes back through ``reply_to``.

Every attempt writes a ``handoffs.jsonl`` row (``gateway/handoff.py``). A person-bound row closes
when the woken turn finishes (``close_after_turn``, called from the gateway's post-turn hooks), not
when the event was queued. ``extra.parent`` links a handoff made inside a woken turn to the one that
woke it, so a chain can be traced; nothing limits chains (no preemptive guards).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

HANDOFFS_FILE = "handoffs.jsonl"


def handoff_store(home: Optional[str] = None) -> Any:
    from gateway.handoff import HandoffStore
    from hermes_constants import get_hermes_home

    return HandoffStore(os.path.join(str(home or get_hermes_home()), HANDOFFS_FILE), author="tell_partner")


def parent_handoff_id(store: Any, session_id: str) -> Optional[str]:
    """The handoff currently waking ``session_id`` (still open while its turn runs), if any."""
    try:
        pending = store.open_for(session_id)
    except Exception:
        return None
    return pending[-1].id if pending else None


def human_notice(handoff_id: str, requester: str, partner: str, intent: str) -> str:
    who = requester or "someone"
    return (f"[Handoff {handoff_id[:8]} · from your conversation with {who}]\n{intent}\n\n"
            f"(This came from another of your conversations, not from {partner}. Act on it in your own "
            f"words and say where it came from. If there is nothing to tell {partner}, reply with exactly "
            "[SILENT].)")


def agent_notice(self_agent: str, requester: str, intent: str) -> str:
    where = f"{self_agent}'s conversation with {requester}" if requester else f"{self_agent}"
    return f"[Handoff from {where}]\n{intent}"


def primary_entry(entries: Any, platform: Any, primary: dict, profile: Optional[str]) -> Any:
    """The routing entry live in a person's primary chat: the most recently active one whose origin is
    that chat (``thread_id`` / ``user_id`` narrow it when the directory gives them), or None.
    Matching the stored origin rather than rebuilding a session key keeps chat type and per-user
    group isolation out of the directory."""
    want_profile = (profile or "").strip() or "default"
    for entry in sorted(entries, key=lambda e: e.updated_at, reverse=True):
        origin = getattr(entry, "origin", None)
        if origin is None or not getattr(entry, "session_id", None):
            continue
        if origin.platform != platform or str(origin.chat_id or "") != primary["chat_id"]:
            continue
        if any(primary.get(k) and str(getattr(origin, k, "") or "") != primary[k] for k in ("thread_id", "user_id")):
            continue
        if ((getattr(origin, "profile", None) or "").strip() or "default") != want_profile:
            continue
        return entry
    return None


async def deliver_to_human(runner: Any, *, partner: str, primary: dict, intent: str, from_session: str,
                           requester: str, profile: Optional[str], home: str) -> dict:
    """Queue the intent into ``partner``'s primary chat session. Returns the tool result dict."""
    from gateway.config import Platform
    from gateway.handoff import DEFERRED
    from gateway.wake import adapter_supports_push, admit_internal_event

    try:
        platform = Platform(primary["platform"])
    except ValueError:
        return {"error": f"{partner}'s primary platform {primary['platform']!r} isn't a gateway platform."}
    adapters = runner._adapters_for_profile(profile)
    adapter = adapters.get(platform)
    if adapter is None or not adapter_supports_push(adapter):
        return {"error": f"This gateway has no {platform.value} connection to reach {partner} on."}
    entry = primary_entry(runner.session_store.list_sessions(), platform, primary, profile)
    if entry is None:
        return {"error": f"{partner} has no conversation in that chat yet (NO_PRIMARY). They have to message "
                         "this agent there once before it can reach them."}
    if entry.session_id == from_session:
        return {"error": f"This IS your conversation with {partner}; just tell them here."}
    store = handoff_store(home)
    handoff = store.open_handoff(
        from_session=from_session, to_session=entry.session_id, requesting_user=requester or "unknown",
        intent=intent, extra={"partner": partner, "kind": "human", "to_key": entry.session_key,
                              "parent": parent_handoff_id(store, from_session)})
    event = runner._synthetic_prompt_event(entry.origin, human_notice(handoff.id, requester, partner, intent),
                                           internal=True)
    event._hermes_handoff_id = handoff.id
    event._hermes_handoff_home = home
    try:
        await admit_internal_event(adapter, event)  # queued behind a running turn counts as accepted
    except Exception as exc:
        store.record(handoff.id, DEFERRED, reason=f"DELIVERY_FAILED: {exc}"[:300])
        return {"error": f"Could not reach {partner}'s conversation: {exc}", "handoff_id": handoff.id}
    return {"success": True, "handoff_id": handoff.id, "partner": partner, "status": "queued",
            "note": f"{partner}'s conversation will act on it next; it closes as delivered when that turn ends."}


def deliver_to_agent(*, partner: str, peer_target: str, intent: str, from_session: str, requester: str,
                     self_agent: str, home: str) -> dict:
    """Send the intent into this agent's session on the partner agent. Returns the tool result dict."""
    import urllib.error
    from gateway.handoff import DEFERRED, DELIVERED
    from hermes_cli.subcommands.peer import send_to_peer

    store = handoff_store(home)
    handoff = store.open_handoff(
        from_session=from_session or "unknown", to_session=f"peer:{peer_target}",
        requesting_user=requester or "unknown", intent=intent,
        extra={"partner": partner, "kind": "agent", "parent": parent_handoff_id(store, from_session)})
    try:
        sent = send_to_peer(peer_target, agent_notice(self_agent or "another agent", requester, intent))
    except (ValueError, LookupError, PermissionError) as exc:
        store.record(handoff.id, DEFERRED, reason=f"NOT_SENT: {exc}"[:300])
        return {"error": str(exc), "handoff_id": handoff.id}
    except TimeoutError as exc:
        store.record(handoff.id, DEFERRED, reason=f"TIMEOUT: {exc}"[:300])
        return {"error": f"{partner} didn't answer within the accept timeout; it may still be working on it.",
                "handoff_id": handoff.id}
    except (urllib.error.URLError, OSError, RuntimeError) as exc:
        store.record(handoff.id, DEFERRED, reason=f"UNREACHABLE: {exc}"[:300])
        return {"error": f"Could not reach {partner}: {exc}", "handoff_id": handoff.id}
    # ACCEPT IS NOT DELIVERY. send_to_peer posts with {"wait": False} and returns
    # on HTTP 202 -- its own docstring says "without waiting for the peer's turn".
    # Recording DELIVERED here made the row unable to distinguish a handoff that
    # produced a reply from one the peer accepted and dropped. That is the defect
    # PR #29 removed from the peer path, and the sibling twelve lines up already
    # gets it right: deliver_to_human returns "queued" and lets close_after_turn
    # close it. Measured cost, 2026-09-19: fifteen rows read as open while every
    # one had run a turn, and the store was believed over the log.
    #
    # An older peer that ran the turn inline DID deliver -- content came back, so
    # the turn demonstrably happened. That case alone keeps DELIVERED.
    reply = sent.get("reply")
    if reply:
        store.record(handoff.id, DELIVERED)
        return {"success": True, "handoff_id": handoff.id, "partner": partner, "status": "delivered",
                "note": f"{partner} ran the turn and replied.", "reply": reply}
    return {"success": True, "handoff_id": handoff.id, "partner": partner, "status": "queued",
            "note": f"{partner}'s gateway accepted it; it closes as delivered when their turn ends."}


def close_after_turn(event: Any, agent_result: Any) -> None:
    """Close the handoff that woke this turn: delivered, or deferred if the turn failed."""
    handoff_id = getattr(event, "_hermes_handoff_id", None)
    if not handoff_id:
        return
    from gateway.handoff import DEFERRED, DELIVERED

    store = handoff_store(getattr(event, "_hermes_handoff_home", None))
    # A streamed turn returns None (already delivered); only an explicit failure defers.
    failed = isinstance(agent_result, dict) and bool(agent_result.get("failed"))
    try:
        if failed:
            store.record(handoff_id, DEFERRED, reason="TURN_FAILED")
        else:
            store.record(handoff_id, DELIVERED)
    except Exception as exc:
        logger.debug("handoff %s could not be closed: %s", handoff_id, exc)

