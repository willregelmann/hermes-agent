"""Durable record of cross-session handoffs.

WHY THIS EXISTS
---------------
A Hermes install serving several people keys one long-lived session per user
pair, and those sessions cannot reach each other. Anything one session learns
that another session's human needs has no path except the human. Observed in
production over four nights: the session serving one household member
diagnosed a fault that needed the other, and its resolution each time was to
ask her to tell him. A missing capability does not announce itself -- it shows
up as a slightly odd habit nobody questions.

WHAT A HANDOFF IS, AND IS NOT
-----------------------------
A handoff carries an INTENT -- prose describing what the target session should
raise with its own human -- and never a message body. The target session
composes its own words from its own context. This module therefore stores no
message text and offers no way to supply any.

That constraint is the whole design. An earlier draft had the source session
compose the message and mirror it into the target's transcript; the tell that
it was wrong is that the mirror had to claim ``role="user"`` to satisfy strict
alternation, writing into a transcript something that was neither that
session's turn nor the agent's. See issue #4.

WHY A RECORD AT ALL, SEPARATE FROM THE WAKE
-------------------------------------------
So that a handoff which does not arrive leaves evidence. A wake can be
refused -- target leased, compression in flight, peer unreachable -- and the
refusal must be visible as a row with a reason rather than as silence. The
same rule the observer runs on: a missing row is itself the alarm, which is
only true if every attempt writes one.

Append-only JSONL. Status transitions are appended as new rows keyed by the
same handoff id rather than rewritten in place, so the history of an attempt
survives and a truncated write can never destroy an earlier record.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Literal, Optional

__all__ = [
    "Handoff",
    "HandoffStore",
    "HandoffStatusError",
    "HandoffStoreDamaged",
    "OPEN",
    "DELIVERED",
    "DEFERRED",
    "FAILED",
]

OPEN = "open"
DELIVERED = "delivered"
DEFERRED = "deferred"
FAILED = "failed"

Status = Literal["open", "delivered", "deferred", "failed"]

#: Terminal statuses. A handoff that reached one of these is closed; recording
#: a further transition on it is a bug in the caller, not a state to accept.
_TERMINAL = frozenset({DELIVERED, FAILED})

#: Legal transitions. ``deferred`` is deliberately NOT terminal: a wake refused
#: because the target was busy is expected to be retried, and each attempt
#: appends its own row so the refusals are countable.
_ALLOWED = {
    OPEN: {DELIVERED, DEFERRED, FAILED},
    DEFERRED: {DELIVERED, DEFERRED, FAILED},
}


class HandoffStatusError(RuntimeError):
    """An illegal status transition was attempted."""


class HandoffStoreDamaged(RuntimeError):
    """The log contains unreadable lines, so an audit answer is not available.

    Distinct from ``HandoffStatusError``: nothing the caller did is wrong, the
    DATA is unreadable. Raised only by readers whose answer would otherwise be
    confidently incomplete.
    """


@dataclass(frozen=True)
class Handoff:
    """One handoff attempt.

    ``intent`` is prose for the TARGET session to act on, never text to send.
    There is no message-body field and adding one would reintroduce the
    rejected design (issue #4).
    """

    id: str
    ts: float
    from_session: str
    to_session: str
    requesting_user: str
    intent: str
    status: Status = OPEN
    reason: Optional[str] = None
    delivered_ts: Optional[float] = None
    #: Free-form provenance for whoever wrote the row (agent name, host).
    author: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> "Handoff":
        d = json.loads(line)
        known = {f for f in cls.__dataclass_fields__}          # noqa: SLF001
        unknown = {k: v for k, v in d.items() if k not in known}
        kept = {k: v for k, v in d.items() if k in known}
        if unknown:
            # Forward compatibility: a newer writer's fields must not make an
            # older reader drop the row entirely. Park them rather than raise.
            kept.setdefault("extra", {}).update(unknown)
        return cls(**kept)


class HandoffStore:
    """Append-only JSONL store of handoff attempts.

    The path is REQUIRED and never inferred. A component that accepts an
    explicit subject but resolves its state from an ambient default can write
    into production while pointed at a fixture -- a defect this codebase has
    already paid for more than once.
    """

    def __init__(self, path: str | os.PathLike, *, author: Optional[str] = None):
        if not path:
            raise ValueError("HandoffStore requires an explicit path")
        self.path = Path(path)
        self.author = author

    # -- writing ---------------------------------------------------------

    def open_handoff(
        self,
        *,
        from_session: str,
        to_session: str,
        requesting_user: str,
        intent: str,
    ) -> Handoff:
        """Record a new handoff in ``open`` state and return it."""
        for name, val in (
            ("from_session", from_session),
            ("to_session", to_session),
            ("requesting_user", requesting_user),
            ("intent", intent),
        ):
            if not val or not str(val).strip():
                raise ValueError(f"{name} is required and must be non-empty")
        if from_session == to_session:
            # Not merely pointless: a self-handoff would wake the session that
            # is already running, which the lease must refuse anyway. Fail here
            # where the error names the cause.
            raise ValueError("from_session and to_session must differ")

        h = Handoff(
            id=uuid.uuid4().hex,
            ts=time.time(),
            from_session=from_session,
            to_session=to_session,
            requesting_user=requesting_user,
            intent=intent,
            status=OPEN,
            author=self.author,
        )
        self._append(h)
        return h

    def record(
        self,
        handoff_id: str,
        status: Status,
        *,
        reason: Optional[str] = None,
    ) -> Handoff:
        """Append a status transition for ``handoff_id``.

        ``reason`` is REQUIRED for every non-delivered outcome. A deferred or
        failed handoff with no reason is indistinguishable from one nobody
        looked at, which defeats the point of writing the row.
        """
        if status not in (DELIVERED, DEFERRED, FAILED):
            raise HandoffStatusError(f"cannot transition to {status!r}")
        if status != DELIVERED and not (reason or "").strip():
            raise HandoffStatusError(f"status {status!r} requires a reason")

        current = self.get(handoff_id)
        if current is None:
            raise HandoffStatusError(f"unknown handoff id {handoff_id!r}")
        if current.status in _TERMINAL:
            raise HandoffStatusError(
                f"handoff {handoff_id!r} is already {current.status!r}; "
                "terminal states do not transition"
            )
        if status not in _ALLOWED.get(current.status, frozenset()):
            raise HandoffStatusError(
                f"illegal transition {current.status!r} -> {status!r}"
            )

        updated = Handoff(
            id=current.id,
            ts=time.time(),
            from_session=current.from_session,
            to_session=current.to_session,
            requesting_user=current.requesting_user,
            intent=current.intent,
            status=status,
            reason=reason,
            delivered_ts=time.time() if status == DELIVERED else None,
            author=self.author,
            extra=dict(current.extra),
        )
        self._append(updated)
        return updated

    def _append(self, h: Handoff) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = h.to_json() + "\n"
        # Single write of a single line: O_APPEND makes concurrent appends from
        # two processes atomic up to PIPE_BUF, and both agents may write here.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)

    # -- reading ---------------------------------------------------------

    def _rows(self, *, damage: Optional[list] = None) -> Iterator[Handoff]:
        """Yield parseable rows; append (line_no, excerpt) to ``damage`` for the rest.

        A corrupt line must not hide the rest of the log, so it is still
        skipped. But skipping SILENTLY was wrong, and Ash caught it in review:
        for an audit surface, a corrupt row is indistinguishable from a row
        that was never written. If the damaged line was a handoff's only
        ``open`` row, that handoff disappears from ``open_for()`` AND from
        ``stale()`` — it is not merely unreadable, it stops being owed. That is
        precisely the disappearance this store exists to make impossible.

        So damage is COUNTED and surfaced (see ``damage_report`` and
        ``stale()``). Callers that do not care pass nothing and behave as
        before; callers that audit ask for it and can say "N rows unreadable"
        rather than reporting a clean empty result.
        """
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield Handoff.from_json(line)
                except Exception:
                    if damage is not None:
                        damage.append((lineno, line[:120]))
                    continue

    def damage_report(self) -> list:
        """Unreadable lines as (line_no, excerpt). Empty list means clean.

        Exists so "no open handoffs" and "no READABLE open handoffs" are
        distinguishable by a caller, which is the whole difference between a
        log and an audit.
        """
        damage: list = []
        for _ in self._rows(damage=damage):
            pass
        return damage

    def get(self, handoff_id: str) -> Optional[Handoff]:
        """Latest row for this id, or None."""
        latest: Optional[Handoff] = None
        for h in self._rows():
            if h.id == handoff_id:
                latest = h
        return latest

    def all_latest(self) -> list[Handoff]:
        """Latest row per handoff id, oldest first by that row's timestamp."""
        by_id: dict[str, Handoff] = {}
        for h in self._rows():
            by_id[h.id] = h
        return sorted(by_id.values(), key=lambda h: h.ts)

    def open_for(self, to_session: str) -> list[Handoff]:
        """Handoffs awaiting delivery to ``to_session``.

        This is what a session reads on wake. ``deferred`` is included: it
        means an earlier attempt was refused, not that the request was
        withdrawn.
        """
        return [
            h for h in self.all_latest()
            if h.to_session == to_session and h.status in (OPEN, DEFERRED)
        ]

    def stale(self, older_than_s: float, *, now: Optional[float] = None) -> list[Handoff]:
        """Undelivered handoffs older than ``older_than_s``.

        A record only audits if something reads it; without a staleness query
        this store is a log, not an audit.

        RAISES ``HandoffStoreDamaged`` when any line is unreadable. This is the
        one reader that must not return a tidy answer over a damaged log: its
        entire purpose is to answer "what is owed and overdue", and a corrupt
        row may BE the overdue thing. Returning a short list would be the
        confident-wrong-answer failure, so the caller is forced to see the
        damage. Use ``damage_report()`` to inspect, or ``all_latest()`` if a
        best-effort view is genuinely what you want.
        """
        damage: list = []
        rows = list(self._rows(damage=damage))
        if damage:
            raise HandoffStoreDamaged(
                f"{len(damage)} unreadable line(s) in {self.path} "
                f"(first at line {damage[0][0]}): staleness cannot be computed "
                f"over a damaged log — an unreadable row may be the overdue one"
            )
        by_id: dict[str, Handoff] = {}
        for h in rows:
            by_id[h.id] = h
        t = now if now is not None else time.time()
        return [h for h in sorted(by_id.values(), key=lambda h: h.ts)
                if h.status in (OPEN, DEFERRED) and (t - h.ts) > older_than_s]
