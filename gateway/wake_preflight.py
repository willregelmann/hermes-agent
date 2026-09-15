"""Ordered preflight for waking an existing session, and the wake itself.

THREE GATES, MEASURED BEFORE THIS MODULE WAS WRITTEN (~/i31-wake/gate{1,2,3}_*.py,
all pass against real code as of 2026-09-14):

GATE 1 — system-prompt byte-stability. Waking is NOT inherently cache-destructive:
appending a turn leaves the static system prefix byte-identical (that is what every
human turn already does). The real hazard is an INDEPENDENTLY CONSTRUCTED system
prompt — if the waker builds its own AIAgent, a one-byte divergence moves the
prefix and the miss lands on the NEXT HUMAN TURN too. This module does not build a
prompt at all: it delivers through ``gateway.wake.deliver_wake``, which either
injects a synthetic MessageEvent through the adapter's own ``handle_message`` (the
same path a real inbound message takes) or self-POSTs the API server (same entry
point real turns use). Gate 1's requirement — reuse the session's own construction
— is satisfied by NOT duplicating it, not by reproducing it correctly. No new test
of byte-equality is needed because there is no second construction to compare.

GATE 2 — session lease. ``active_sessions.try_acquire_active_session(...,
track_liveness=True)`` before touching the session. Per-session exclusivity is
unconditional (#94595) — this is not optional even without a concurrency cap
configured. A refusal carries ``.reason`` (SESSION_NOT_OWNED /
SESSION_COORDINATION_UNAVAILABLE) which becomes the handoff row's reason verbatim,
never re-derived from prose.

GATE 3 — compression in flight. ``runner._session_has_compression_in_flight(...)``
(gateway/run.py:10732) is fail-SAFE: any read error is treated as True (compression
assumed active) rather than False. A wake must never interrupt a mid-rotation
session (#56391 orphaned-sibling hazard), so this gate runs with the lease already
held and releases it on refusal.

RECORD, NEVER REFUSES: gateway.handoff.HandoffStore. The WAKE refuses; the record
never does. Every attempt gets an ``open`` row. Gate 2 refusal -> deferred with its
own .reason. Gate 3 True -> deferred with reason=COMPRESSION_IN_FLIGHT (lease
released first — a refused wake must not hold a lease it never used). Delivery
success -> delivered. Delivery failure (deliver_wake raises) -> deferred with
reason=DELIVERY_FAILED, lease released, exception re-raised so the caller sees it.

DESIGN INVARIANT, unchanged from the original review that started this work: wake
takes (session_id, prompt) — an INTENT for the target session to act on, never a
message body it is instructed to say. The handoff intent and the wake prompt are
the same text for exactly this reason; there is no second channel that could drift
from it. A woken session states its own origin in its own words.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from gateway.handoff import DELIVERED, DEFERRED, Handoff, HandoffStore
from gateway.wake import deliver_wake
from hermes_cli.active_sessions import (
    ActiveSessionLease,
    try_acquire_active_session,
)

logger = logging.getLogger(__name__)

#: Not an active_sessions.py reason — compression-in-flight is this module's own
#: gate, not the lease's, so it gets its own constant rather than overloading one
#: that means something else in the registry's vocabulary.
COMPRESSION_IN_FLIGHT = "COMPRESSION_IN_FLIGHT"
#: The delivery step (gateway.wake.deliver_wake) raised after both gates passed.
DELIVERY_FAILED = "DELIVERY_FAILED"


class WakeRefused(RuntimeError):
    """A gate refused the wake. ``.handoff`` carries the deferred row."""

    def __init__(self, message: str, handoff: Handoff) -> None:
        super().__init__(message)
        self.handoff = handoff


async def wake(
    *,
    runner: Any,
    adapter: Any,
    handoff_store: HandoffStore,
    from_session: str,
    to_session: str,
    requesting_user: str,
    prompt: str,
    surface: str = "wake",
    source: Any = None,
    config: Any = None,
) -> Handoff:
    """Wake ``to_session`` with ``prompt`` through gates 2 and 3, recorded throughout.

    ``prompt`` is the SAME text as the handoff's ``intent`` — see module docstring.
    Returns the terminal ``Handoff`` row on success (status=delivered). Raises
    ``WakeRefused`` on a gate refusal or ``DELIVERY_FAILED``; ``exc.handoff`` is the
    deferred row already written, so a caller never has to re-derive what happened.

    Gate 1 is not a runtime check here — see module docstring for why.
    """
    h = handoff_store.open_handoff(
        from_session=from_session,
        to_session=to_session,
        requesting_user=requesting_user,
        intent=prompt,
    )

    # --- GATE 2: session lease (per-session exclusivity, unconditional) ----
    lease, refusal = try_acquire_active_session(
        session_id=to_session,
        surface=surface,
        config=config,
        track_liveness=True,
    )
    if refusal is not None:
        deferred = handoff_store.record(h.id, DEFERRED, reason=str(refusal.reason))
        logger.info(
            "wake %s deferred at gate 2 (%s): %s", h.id, refusal.reason, refusal
        )
        raise WakeRefused(str(refusal), deferred)

    assert lease is not None  # try_acquire_active_session guarantees exactly one

    try:
        # --- GATE 3: compression in flight (fail-safe: errors -> True) ----
        in_flight = await runner._session_has_compression_in_flight(to_session)
        if in_flight:
            deferred = handoff_store.record(
                h.id, DEFERRED, reason=COMPRESSION_IN_FLIGHT
            )
            logger.info(
                "wake %s deferred at gate 3: compression in flight for %s",
                h.id,
                to_session,
            )
            raise WakeRefused(
                f"compression in flight for session {to_session}", deferred
            )

        # --- DELIVERY -------------------------------------------------------
        try:
            await deliver_wake(
                adapter, text=prompt, session_id=to_session, source=source
            )
        except Exception as exc:
            deferred = handoff_store.record(
                h.id, DEFERRED, reason=DELIVERY_FAILED
            )
            logger.warning(
                "wake %s delivery failed for %s: %s", h.id, to_session, exc
            )
            raise WakeRefused(f"delivery failed: {exc}", deferred) from exc

        delivered = handoff_store.record(h.id, DELIVERED)
        logger.info("wake %s delivered to %s", h.id, to_session)
        return delivered
    finally:
        _release(lease)


def _release(lease: ActiveSessionLease) -> None:
    """Best-effort lease release; a wake that already ended must not hold it.

    Mirrors the tolerant-release pattern used at every other active_sessions.py
    call site (#94595) — a release failure must not mask the wake's real outcome,
    which has already been recorded by the time this runs.
    """
    release = getattr(lease, "release", None)
    if not callable(release):
        return
    try:
        release()
    except Exception:
        logger.warning("wake: lease release failed for %s", lease, exc_info=True)
