"""The partner directory: who an agent talks to, and where each partner's primary session is (#52).

Per-profile, in ``config.yaml``::

    partners:
      will:   {kind: human, primary: {platform: google_chat, chat_id: spaces/AAAA}}
      britta: {kind: human, primary: {platform: google_chat, chat_id: spaces/BBBB}}
      ash:    {kind: agent, peer: ash}        # a bot_peers target: <peer> or <peer>/<profile>

A person's primary session is whatever session is live in that chat when a handoff happens (it
follows compression and ``/new``). Another agent's primary is the session on that agent titled
``Peer: <this agent>``. Nothing here stores a session id. See ``docs/capabilities/tell-partner.md``.
"""

from __future__ import annotations

from typing import Dict, Optional

HUMAN, AGENT = "human", "agent"
PEER_SESSION_TITLE_PREFIX = "Peer: "


def load_partners() -> Dict[str, dict]:
    """Normalized ``{name: entry}`` for the current profile; malformed entries are skipped."""
    from hermes_cli.config import load_config

    raw = (load_config() or {}).get("partners")
    out: Dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for name, entry in raw.items():
        key = str(name or "").strip().lower()
        if not key or not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or HUMAN).strip().lower()
        if kind == AGENT and str(entry.get("peer") or "").strip():
            out[key] = {"kind": AGENT, "peer": str(entry["peer"]).strip()}
        elif kind == HUMAN and isinstance(entry.get("primary"), dict):
            primary = {k: str(v).strip() for k, v in entry["primary"].items() if v is not None}
            if primary.get("platform") and primary.get("chat_id"):
                out[key] = {"kind": HUMAN, "primary": primary}
    return out


def roster_line(partners: Optional[Dict[str, dict]] = None) -> str:
    """``Your partners: will (person), ash (agent).`` for the system prompt, built once per session."""
    table = load_partners() if partners is None else partners
    if not table:
        return ""
    kinds = {HUMAN: "person", AGENT: "agent"}
    return "Your partners: " + ", ".join(f"{n} ({kinds[e['kind']]})" for n, e in sorted(table.items())) + "."


def own_agent_name() -> str:
    """This agent's name as peers know it (``identity.json``), else ""."""
    from hermes_cli.subcommands.peer import _self_origin

    origin = _self_origin() or {}
    return str(origin.get("agent") or "").strip()


def peer_session_title(agent_name: str) -> str:
    return f"{PEER_SESSION_TITLE_PREFIX}{agent_name}"


def partner_for_session(session_id: str, platform: str = "", chat_id: str = "",
                        partners: Optional[Dict[str, dict]] = None) -> str:
    """Which partner a session belongs to: a human whose primary chat it is, or the agent named by a
    ``Peer: <agent>`` title. "" when it is neither (e.g. a CLI side session)."""
    table = load_partners() if partners is None else partners
    for name, entry in table.items():
        primary = entry.get("primary") or {}
        if entry["kind"] == HUMAN and platform and primary.get("platform") == platform \
                and primary.get("chat_id") == chat_id:
            return name
    title = _session_title(session_id)
    if title.startswith(PEER_SESSION_TITLE_PREFIX):
        agent = title[len(PEER_SESSION_TITLE_PREFIX):].strip()
        for name, entry in table.items():
            if entry["kind"] == AGENT and entry["peer"].split("/", 1)[0].lower() == agent.lower():
                return name
        return agent
    return ""


def _session_title(session_id: str) -> str:
    if not session_id:
        return ""
    try:
        from hermes_cli.goals import _get_session_db
        db = _get_session_db()
        return str((db.get_session_title(session_id) if db is not None else "") or "")
    except Exception:
        return ""
