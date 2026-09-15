"""Tests for gateway/wake_preflight.py — the wake() primitive shipped in PR #13.

Committed regression debt named in Wren's PR #13 review (approved with a named
gap): her 22+8 reproduction cases lived only in ad-hoc scripts on two boxes,
protecting nothing after merge. This file adapts them into a real suite.

The specific gap she called out as most important: lease-release-on-refusal is
the subtle part ("a refused wake must not hold a lease it never used") and is
exactly what a well-meaning refactor breaks silently. Every refusal path below
asserts BOTH the row transition AND the lease release.

Also her methodological rule, carried over: at least one case constructs the
subject the way production does rather than faking every collaborator. That is
not attempted here (this suite fakes the two external collaborators, same as
her original reproduction) — flagged as a residual gap in the PR body, not
hidden.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from gateway.handoff import DEFERRED, DELIVERED, HandoffStore
from gateway import wake_preflight as WP


# --- fakes for the collaborators (gate 2 registry, gate 3 runner, delivery) --

class FakeLease:
    def __init__(self, sid, released_log):
        self.session_id = sid
        self._log = released_log

    def release(self):
        self._log.append(self.session_id)


class FakeRefusal:
    def __init__(self, reason):
        self.reason = reason

    def __str__(self):
        return f"refused: {self.reason}"


class FakeRegistryError(Exception):
    pass


class FakeRunner:
    """Gate 3 stand-in. in_flight can be a bool or an exception instance."""

    def __init__(self, in_flight=False):
        self._in_flight = in_flight
        self.asked = []

    async def _session_has_compression_in_flight(self, sid):
        self.asked.append(sid)
        if isinstance(self._in_flight, Exception):
            raise self._in_flight
        return self._in_flight


@pytest.fixture
def collab(monkeypatch):
    """Bundle of fakes + hooks wired into wake_preflight's module namespace."""
    released = []
    delivered_calls = []
    acquire_mode = {"mode": "grant"}  # grant | refuse | raise | raise_other
    deliver_mode = {"mode": "ok"}

    def fake_try_acquire(*, session_id, surface, config, track_liveness):
        if acquire_mode["mode"] == "refuse":
            return None, FakeRefusal("SESSION_NOT_OWNED")
        if acquire_mode["mode"] == "raise":
            raise FakeRegistryError("registry file is damaged")
        if acquire_mode["mode"] == "raise_other":
            raise OSError("disk full acquiring lock")
        return FakeLease(session_id, released), None

    async def fake_deliver_wake(adapter, *, text, session_id, source=None):
        delivered_calls.append({"text": text, "session_id": session_id})
        if deliver_mode["mode"] == "raise":
            raise RuntimeError("adapter exploded")
        return True

    monkeypatch.setattr(WP, "try_acquire_active_session", fake_try_acquire)
    monkeypatch.setattr(WP, "deliver_wake", fake_deliver_wake)
    # ActiveSessionRegistryError must be the SAME class wake_preflight catches.
    monkeypatch.setattr(WP, "ActiveSessionRegistryError", FakeRegistryError)

    return {
        "released": released,
        "delivered_calls": delivered_calls,
        "acquire_mode": acquire_mode,
        "deliver_mode": deliver_mode,
    }


@pytest.fixture
def store(tmp_path):
    def _make(name):
        return HandoffStore(str(tmp_path / f"{name}.jsonl"), author="test")
    return _make


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


PROMPT = "stair circuit is faulty"


def do_wake(runner, s, adapter=None):
    return run(WP.wake(
        runner=runner, adapter=adapter or object(), handoff_store=s,
        from_session="britta", to_session="will",
        requesting_user="Britta", prompt=PROMPT,
    ))


# --- C1: gate-2 clean refusal ----------------------------------------------

def test_gate2_refusal_defers_with_own_reason(collab, store):
    collab["acquire_mode"]["mode"] = "refuse"
    s = store("c1")
    with pytest.raises(WP.WakeRefused) as ei:
        do_wake(FakeRunner(), s)
    rows = s.all_latest()
    assert len(rows) == 1
    assert rows[0].status == DEFERRED
    assert rows[0].reason == "SESSION_NOT_OWNED"
    assert ei.value.handoff.id == rows[0].id
    assert not collab["delivered_calls"]


def test_gate2_called_with_track_liveness_true(collab, store):
    """Non-vacuity: gate 2 must actually be consulted with liveness tracking on."""
    calls = []
    orig = collab["acquire_mode"]

    async def _noop():
        pass

    def spy(*, session_id, surface, config, track_liveness):
        calls.append(track_liveness)
        return None, FakeRefusal("SESSION_NOT_OWNED")

    import gateway.wake_preflight as wp
    wp.try_acquire_active_session = spy
    s = store("c1b")
    with pytest.raises(WP.WakeRefused):
        do_wake(FakeRunner(), s)
    assert calls == [True]


# --- C2: gate-3 refusal — the lease MUST be released ------------------------

def test_gate3_refusal_defers_and_releases_lease(collab, store):
    collab["acquire_mode"]["mode"] = "grant"
    s = store("c2")
    runner = FakeRunner(in_flight=True)
    with pytest.raises(WP.WakeRefused):
        do_wake(runner, s)
    rows = s.all_latest()
    assert rows[0].status == DEFERRED
    assert "COMPRESSION" in (rows[0].reason or "").upper()
    assert runner.asked == ["will"]
    assert collab["released"] == ["will"], "refused wake must not hold a lease it never used"
    assert not collab["delivered_calls"]


# --- C3: delivery failure ---------------------------------------------------

def test_delivery_failure_defers_and_releases_lease(collab, store):
    collab["acquire_mode"]["mode"] = "grant"
    collab["deliver_mode"]["mode"] = "raise"
    s = store("c3")
    runner = FakeRunner(in_flight=False)
    with pytest.raises(WP.WakeRefused):
        do_wake(runner, s)
    rows = s.all_latest()
    assert rows[0].status == DEFERRED
    assert "DELIVERY" in (rows[0].reason or "").upper()
    assert len(collab["delivered_calls"]) == 1, "delivery must actually be attempted"
    assert collab["released"] == ["will"]


# --- C4: happy path ----------------------------------------------------------

def test_happy_path_delivers_and_releases_lease(collab, store):
    collab["acquire_mode"]["mode"] = "grant"
    collab["deliver_mode"]["mode"] = "ok"
    s = store("c4")
    runner = FakeRunner(in_flight=False)
    h = do_wake(runner, s)
    assert h.status == DELIVERED
    assert collab["released"] == ["will"]


# --- C5: design invariant — no second channel --------------------------------

def test_delivered_text_equals_recorded_intent(collab, store):
    collab["acquire_mode"]["mode"] = "grant"
    collab["deliver_mode"]["mode"] = "ok"
    s = store("c5")
    do_wake(FakeRunner(in_flight=False), s)
    rows = s.all_latest()
    assert collab["delivered_calls"][0]["text"] == rows[0].intent == PROMPT


def test_wake_signature_has_no_message_body_param():
    import inspect
    sig = inspect.signature(WP.wake).parameters
    assert not ({"message", "body", "text"} & set(sig))


# --- C6: every refusal path leaves exactly one countable row ----------------

@pytest.mark.parametrize("mode,setup", [
    ("gate2_refuse", lambda c: c["acquire_mode"].__setitem__("mode", "refuse")),
    ("gate3_refuse", lambda c: c["acquire_mode"].__setitem__("mode", "grant")),
    ("delivery_fail", lambda c: (
        c["acquire_mode"].__setitem__("mode", "grant"),
        c["deliver_mode"].__setitem__("mode", "raise"),
    )),
])
def test_refusal_never_leaves_a_silent_row(collab, store, mode, setup):
    setup(collab)
    s = store(f"c6-{mode}")
    runner = FakeRunner(in_flight=(mode == "gate3_refuse"))
    with pytest.raises(WP.WakeRefused):
        do_wake(runner, s)
    rows = s.all_latest()
    assert len(rows) == 1
    assert rows[0].status == DEFERRED


# --- C7: gate 2 RAISING must not leave the row stuck at open ----------------

def test_gate2_raising_registry_error_defers_not_stuck_open(collab, store):
    collab["acquire_mode"]["mode"] = "raise"
    s = store("c7")
    runner = FakeRunner(in_flight=False)
    with pytest.raises(WP.WakeRefused):
        do_wake(runner, s)
    rows = s.all_latest()
    assert len(rows) == 1
    assert rows[0].status == DEFERRED, "a raising gate must not leave the row at open"
    assert "REGISTRY" in (rows[0].reason or "").upper()
    assert not collab["delivered_calls"]


def test_gate2_raising_other_exception_still_defers(collab, store):
    """Round-2 case G: a disk-full/lock OSError is a different exception type
    than ActiveSessionRegistryError and must still transition the row."""
    collab["acquire_mode"]["mode"] = "raise_other"
    s = store("c7b")
    runner = FakeRunner(in_flight=False)
    with pytest.raises(WP.WakeRefused):
        do_wake(runner, s)
    rows = s.all_latest()
    assert rows[0].status == DEFERRED
    assert rows[0].reason == WP.GATE_ERROR


# --- C8: gate 3 RAISING must release the lease and not leave row open -------

def test_gate3_raising_defers_and_releases_lease(collab, store):
    collab["acquire_mode"]["mode"] = "grant"
    s = store("c8")
    runner = FakeRunner(in_flight=RuntimeError("compression probe itself failed"))
    with pytest.raises(WP.WakeRefused):
        do_wake(runner, s)
    rows = s.all_latest()
    assert rows[0].status == DEFERRED
    assert collab["released"] == ["will"], "lease must be released even when the gate itself threw"
