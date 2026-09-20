"""A 202 accept is not a delivery.

THE DEFECT, live on 5ee9fc531b at gateway/partner_handoff.py:139:

    sent = send_to_peer(...)        # POST with {"wait": False} -> HTTP 202, no content
    store.record(handoff.id, DELIVERED)
    result = {..., "status": "delivered",
              "note": f"{partner} has it; their answer comes back ..."}

``send_to_peer``'s own docstring says it returns "without waiting for the
peer's turn". So DELIVERED is recorded on ACCEPT. The row can never say
anything else: a handoff that the peer accepted and then dropped on the floor
is byte-identical, in the store, to one that produced a reply.

This is the defect PR #29 removed from the peer path -- ``store.record(
handoff.id, DELIVERED)`` inside ``_run_and_record``, closing on turn
completion rather than real delivery -- reintroduced in the partner path.
Its own sibling twelve lines up already does the right thing:
``deliver_to_human`` returns status "queued" with the note "it closes as
delivered when that turn ends", and ``close_after_turn`` closes it later from
``event._hermes_handoff_id``. (The sibling is the reference for the STATUS
vocabulary only -- this suite asserts on behaviour here, never on that
function's source text.)

WHY IT MATTERS, measured 2026-09-19 on my own store: outbound and inbound are
wrong in OPPOSITE directions, and both readings were confidently wrong.
Outbound over-reports -- at that date all 21 outbound peer rows had reached
DELIVERED, every close written by the sender, median ~0.1s after open: a close
that timed an HTTP round trip, not a turn. Inbound under-reports -- 14 rows sat
at ``open`` while every one of them had demonstrably run a turn (agent.log, and
the replies exist). Reading the store, I told Will a dozen of Ash's replies had
never been delivered. They all had. (Counts are as of that date; re-derive
before quoting them.)

WHAT THIS SUITE PINS, and deliberately not more: the accept path must not
record a terminal DELIVERED, and must not TELL the caller "delivered", when
the transport only got an accept. The synchronous case -- an older peer that
ran the turn inline and returned content -- is genuinely delivered and must
stay so; that distinction is the whole contract.

WHAT IT DOES NOT FIX, and the note must not claim otherwise: nothing on the
sending box closes the row afterwards. ``deliver_to_human`` earns its "queued"
by stamping ``event._hermes_handoff_id`` for ``close_after_turn`` to read back;
there is no such thread here, because ``send_to_peer``'s extra carries only
wait/reply_to/from and the id never leaves this box. The peer opens its own row
under its own id, and the completion watcher closes the RECEIVER's. So after
this change the outbound row stays open -- which is the honest state, since an
accepted-and-dropped handoff genuinely is indistinguishable from an accepted
one until something observes the turn. Closing the loop is a separate change.
"""

from __future__ import annotations

import os
import sys
import tempfile

TREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, TREE)

FAILED: list[str] = []
PASSED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(label)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok and detail:
        print(f"        {detail}")


def _run(monkey_reply: str | None, home: str) -> tuple[dict, list[tuple[str, str]]]:
    """Drive deliver_to_agent with send_to_peer faked, capturing store writes.

    ``monkey_reply`` None  -> accept-only (the real --no-wait shape, HTTP 202)
    ``monkey_reply`` str   -> an older peer that ran the turn synchronously
    """
    from gateway import partner_handoff as ph

    recorded: list[tuple[str, str]] = []

    class _Store:
        def __init__(self) -> None:
            self.id = "h_test"

        def open_handoff(self, **kw):
            return type("H", (), {"id": self.id})()

        def record(self, hid, status, reason=None):
            recorded.append((hid, str(status)))

    import hermes_cli.subcommands.peer as peer_mod

    real_send = getattr(peer_mod, "send_to_peer", None)
    real_store = ph.handoff_store

    def fake_send(peer, text, **kw):
        return {"peer": peer, "accepted": True, "reply": monkey_reply or ""}

    peer_mod.send_to_peer = fake_send
    ph.handoff_store = lambda home=None: _Store()
    try:
        result = ph.deliver_to_agent(
            partner="ash", peer_target="ash", intent="x", from_session="s",
            requester="wren", self_agent="wren", home=home)
    finally:
        ph.handoff_store = real_store
        if real_send is not None:
            peer_mod.send_to_peer = real_send
    return result, recorded


print("=" * 72)
print("a 202 accept is not a delivery")
print("=" * 72)

with tempfile.TemporaryDirectory() as home:
    # --- the live shape: --no-wait, 202, no content ------------------------
    res, rec = _run(None, home)

    check("A1 NON-VACUITY: the accept-only path ran and returned a result",
          isinstance(res, dict) and bool(res.get("handoff_id")),
          f"got {res!r}")

    statuses = [s for _, s in rec]
    check("A2 the store is NOT told DELIVERED on a bare accept",
          not any("deliver" in s.lower() for s in statuses),
          f"store writes were {statuses!r} -- a 202 means the peer's gateway "
          f"took the HTTP request, not that a turn ran or a reply exists")

    check("A3 the CALLER is not told 'delivered' on a bare accept",
          str(res.get("status", "")).lower() != "delivered",
          f"status={res.get('status')!r}; send_to_peer's own docstring says it "
          f"returns without waiting for the peer's turn")

    check("A4 the note does not promise the partner HAS it",
          "has it" not in str(res.get("note", "")).lower(),
          f"note={res.get('note')!r}")

    # --- the synchronous case must NOT be downgraded -----------------------
    res2, rec2 = _run("a real reply body", home)
    check("C1 an older peer that ran the turn inline IS delivered",
          any("deliver" in s.lower() for s in (s for _, s in rec2)),
          f"store writes were {[s for _, s in rec2]!r}; content came back, so "
          f"the turn demonstrably ran -- this one is a real delivery")
    check("C2 ...and that reply is passed through to the caller",
          res2.get("reply") == "a real reply body",
          f"got {res2.get('reply')!r}")

print()
print("=" * 72)
print(f"cases: {len(PASSED) + len(FAILED)}   failed: {len(FAILED)}")
if FAILED:
    for f in FAILED:
        print(f"  RED: {f}")
print("=" * 72)
sys.exit(1 if FAILED else 0)
