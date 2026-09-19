"""Self-wake: one-shot alarms an agent sets for its own session.

Before a turn ends an agent can set an alarm ("in 1h", "at 17:00") with a note to itself. When it
comes due, the SAME session takes another turn that starts from the note. See
``docs/capabilities/self-wake.md`` (#53).

Why this exists, in the words of the failure it fixes (2026-09-15): Wren said "I'll check in an
hour"; asked whether it could actually wait an hour and report back unprompted, the answer was no.
An intention had been stated as though stating it were arranging it. ``set_alarm`` therefore returns
an id that can be checked and raises when nothing was stored, so a promise is backed by a record.

Storage mirrors ``/loop``: ``state_meta`` rows in the session's own ``state.db``, keyed
``alarm:<session_id>:<alarm_id>`` (several per session). Rows are never deleted: status moves
``pending`` -> ``fired`` | ``cancelled`` so what an agent arranged stays auditable.

Firing is driven by whichever process can run a turn in the session, each only while the session is
idle: the gateway (``gateway/run_alarms.py``) for alarms carrying a gateway ``route``, and the CLI /
TUI-desktop backend for route-less alarms of the session they hold. ``claim_alarm`` is a
compare-and-set on the row, so two holders of one session fire an alarm once. An alarm fires only in
the session it was set in; ``/new`` and ``/reset`` cancel it (SessionDB's end stamp), nothing else does.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from hermes_state_common import ALARM_META_PREFIX

logger = logging.getLogger(__name__)

PENDING, FIRED, CANCELLED = "pending", "fired", "cancelled"
# Fired this long after due_at (restart, closed CLI, busy session) the notice says it is late.
LATE_AFTER_SECONDS = 120


class AlarmError(ValueError):
    """An alarm could not be set, parsed or stored."""


@dataclass
class AlarmState:
    alarm_id: str
    session_id: str
    note: str
    due_at: float
    set_at: float
    status: str = PENDING
    fired_at: float = 0.0
    # Gateway routing (platform, chat_id, chat_type, thread_id, user_id, user_name, profile) when
    # the alarm was set in a gateway session; empty for CLI / TUI / desktop sessions, whose own
    # process fires them.
    route: Dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "AlarmState":
        data = json.loads(raw)
        route = data.get("route")
        return cls(
            alarm_id=str(data["alarm_id"]), session_id=str(data["session_id"]),
            note=str(data.get("note") or ""), due_at=float(data["due_at"]),
            set_at=float(data.get("set_at") or 0.0), status=str(data.get("status") or PENDING),
            fired_at=float(data.get("fired_at") or 0.0),
            route={str(k): str(v) for k, v in route.items()} if isinstance(route, dict) else {},
        )

    @property
    def key(self) -> str:
        return _meta_key(self.session_id, self.alarm_id)


def _meta_key(session_id: str, alarm_id: str) -> str:
    return f"{ALARM_META_PREFIX}{session_id}:{alarm_id}"


def _get_session_db() -> Optional[Any]:
    """The goals module's per-home SessionDB cache, shared with /goal and /loop."""
    from hermes_cli.goals import _get_session_db as _goals_db
    return _goals_db()


# ---- when -> due_at ---------------------------------------------------------------------------

_AT_RE = re.compile(r"^at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.IGNORECASE)


def parse_when(when: str, now: Optional[datetime] = None) -> float:
    """Epoch seconds for a one-shot ``when``: ``in 20m`` / ``in 2h`` / ``in 1d``, ``at 17:00`` /
    ``at 5pm`` (next occurrence, Hermes timezone), or an ISO timestamp. Recurring forms are refused:
    a repeating alarm is set again from the woken turn."""
    from hermes_time import now as hermes_now

    text = (when or "").strip()
    current = now or hermes_now()
    lowered = text.lower()
    if not text:
        raise AlarmError("when is required, e.g. 'in 1h', 'in 20m', 'at 17:00'.")
    if lowered.startswith("every ") or re.fullmatch(r"\d+\s*[a-z]+", lowered):
        raise AlarmError(f"'{text}' is recurring; an alarm goes off once. Use 'in 1h' or 'at 17:00', "
                         "and set another from the woken turn if it should repeat.")
    if lowered.startswith("in "):
        from cron.jobs import parse_duration
        try:
            minutes = parse_duration(text[3:])
        except ValueError as exc:
            raise AlarmError(str(exc)) from exc
        return current.timestamp() + minutes * 60
    match = _AT_RE.match(text)
    if match:
        hour, minute, meridiem = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or "").lower()
        if meridiem:
            if not 1 <= hour <= 12:
                raise AlarmError(f"Invalid time '{text}'.")
            hour = hour % 12 + (12 if meridiem == "pm" else 0)
        if hour > 23 or minute > 59:
            raise AlarmError(f"Invalid time '{text}'.")
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= current:
            target += timedelta(days=1)
        return target.timestamp()
    from cron.jobs import parse_schedule
    try:
        parsed = parse_schedule(text)
    except ValueError as exc:
        raise AlarmError(f"Can't read '{text}' as a time. Use 'in 1h', 'at 17:00' or an ISO timestamp.") from exc
    if parsed.get("kind") != "once":
        raise AlarmError(f"'{text}' is recurring; an alarm goes off once.")
    return datetime.fromisoformat(parsed["run_at"]).timestamp()


# ---- store ------------------------------------------------------------------------------------

def _require_db() -> Any:
    db = _get_session_db()
    if db is None:
        raise AlarmError("The session store is unavailable; the alarm was not set.")
    return db


def set_alarm(session_id: str, when: str, note: str, *, route: Optional[Dict[str, str]] = None) -> AlarmState:
    """Store a pending alarm for ``session_id`` and return it. Raises AlarmError if nothing was stored."""
    if not (session_id or "").strip():
        raise AlarmError("No session to wake: an alarm needs the id of the session it belongs to.")
    note = (note or "").strip()
    if not note:
        raise AlarmError("note is required: tell your future self why it is waking up.")
    due_at = parse_when(when)
    state = AlarmState(
        alarm_id=f"a_{uuid.uuid4().hex[:6]}", session_id=session_id.strip(), note=note,
        due_at=due_at, set_at=time.time(), route={k: v for k, v in (route or {}).items() if v},
    )
    db = _require_db()
    db.set_meta(state.key, state.to_json())
    stored = db.get_meta(state.key)
    if stored != state.to_json():
        raise AlarmError("The alarm could not be confirmed in the session store; it was not set.")
    return state


def _parse(raw: str) -> Optional[AlarmState]:
    try:
        return AlarmState.from_json(raw)
    except Exception as exc:
        logger.debug("alarms: unreadable row skipped: %s", exc)
        return None


def _rows(prefix: str) -> List[AlarmState]:
    db = _get_session_db()
    if db is None:
        return []
    try:
        rows = db.list_meta_prefix(prefix)
    except Exception as exc:
        logger.debug("alarms: list failed: %s", exc)
        return []
    return [a for a in (_parse(raw) for _key, raw in rows if raw) if a is not None]


def list_alarms(session_id: str, *, include_done: bool = False) -> List[AlarmState]:
    """Alarms of one session, soonest first (pending only unless ``include_done``)."""
    alarms = _rows(f"{ALARM_META_PREFIX}{session_id}:") if session_id else []
    return sorted((a for a in alarms if include_done or a.status == PENDING), key=lambda a: a.due_at)


def list_due_alarms(now: Optional[float] = None) -> List[AlarmState]:
    """Every pending alarm in this home's store that is due, soonest first."""
    at = time.time() if now is None else now
    return sorted((a for a in _rows(ALARM_META_PREFIX) if a.status == PENDING and a.due_at <= at),
                  key=lambda a: a.due_at)


def due_alarms_for_session(session_id: str, now: Optional[float] = None) -> List[AlarmState]:
    at = time.time() if now is None else now
    return [a for a in list_alarms(session_id) if a.due_at <= at]


def _transition(alarm: AlarmState, expected_status: str, **changes: Any) -> Optional[AlarmState]:
    """Compare-and-set the stored row from ``expected_status``; the new state, or None if another
    holder moved it first (or it no longer exists)."""
    db = _get_session_db()
    if db is None:
        return None
    raw = db.get_meta(alarm.key)
    current = _parse(raw) if raw else None
    if current is None or current.status != expected_status:
        return None
    updated = AlarmState(**{**asdict(current), **changes})
    return updated if db.compare_and_set_meta(alarm.key, raw, updated.to_json()) else None


def claim_alarm(alarm: AlarmState, now: Optional[float] = None) -> Optional[AlarmState]:
    """Mark a pending alarm fired; exactly one caller wins. The winner injects the wake turn."""
    return _transition(alarm, PENDING, status=FIRED, fired_at=time.time() if now is None else now)


def release_alarm(alarm: AlarmState) -> Optional[AlarmState]:
    """Undo a claim whose wake turn could not be started, so the alarm stays due."""
    return _transition(alarm, FIRED, status=PENDING, fired_at=0.0)


def cancel_alarm(session_id: str, alarm_id: str) -> bool:
    """Cancel one pending alarm of ``session_id``; False when there is no such pending alarm."""
    raw = None
    db = _get_session_db()
    if db is not None:
        raw = db.get_meta(_meta_key(session_id, alarm_id))
    current = _parse(raw) if raw else None
    return bool(current and _transition(current, PENDING, status=CANCELLED))


def migrate_alarms_to_session(old_session_id: str, new_session_id: str) -> int:
    """Carry pending alarms across a compression rotation (legacy ``compression.in_place: false``)."""
    if not old_session_id or not new_session_id or old_session_id == new_session_id:
        return 0
    moved = 0
    db = _get_session_db()
    for alarm in list_alarms(old_session_id):
        if _transition(alarm, PENDING, status=CANCELLED) is None or db is None:
            continue
        carried = AlarmState(**{**asdict(alarm), "session_id": new_session_id})
        db.set_meta(carried.key, carried.to_json())
        moved += 1
    return moved


# ---- the wake turn ----------------------------------------------------------------------------

def _span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)}m"
    if seconds < 36 * 3600:
        return f"{seconds / 3600:.1f}h".replace(".0h", "h")
    return f"{seconds / 86400:.1f}d".replace(".0d", "d")


def wake_notice(alarm: AlarmState, now: Optional[float] = None, *, reply_goes_to: str = "") -> str:
    """The user-role text that starts the woken turn: marked as the agent's own alarm, never as a
    message from the partner, with lateness stated when it fires late. ``reply_goes_to`` names the
    agent a reply is sent to when that isn't obvious (an agent-pair session)."""
    at = time.time() if now is None else now
    late = at - alarm.due_at
    late_note = (f" · firing {_span(late)} late (nothing could run this session when it came due)"
                 if late > LATE_AFTER_SECONDS else "")
    goes_to = f"Your reply is sent to {reply_goes_to}. " if reply_goes_to else ""
    return (f"[Alarm {alarm.alarm_id} · you set this {_span(at - alarm.set_at)} ago in this conversation"
            f"{late_note}]\n{alarm.note}\n\n"
            f"(This is your own alarm, not a message from anyone. {goes_to}If there is nothing worth "
            "saying, reply with exactly [SILENT].)")
