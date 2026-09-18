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


# --- Wren's PR review case: the collab fixture monkeypatches WP's own
# ActiveSessionRegistryError to make the raising cases work above, which
# means no case in this file notices if wake_preflight ever caught a class
# hermes_cli.active_sessions doesn't actually raise. A fake standing in for
# the linkage cannot test the linkage -- so this one case takes no fixture
# on purpose and checks the real symbol identity.

def test_gate2_catches_the_class_active_sessions_actually_raises():
    """collab monkeypatches WP.ActiveSessionRegistryError, so no other case
    can notice if wake_preflight catches a class active_sessions never
    raises. This case takes no fixture on purpose."""
    import hermes_cli.active_sessions as real_as
    assert WP.ActiveSessionRegistryError is real_as.ActiveSessionRegistryError


# ===========================================================================
# Round 18 (Wren, mutation audit of merged PR #13 + this suite).  Six mutants
# survived all 14 cases above; each block below is one named arm per mutant.
# ===========================================================================


# --- D1: the wake must be DELIVERED TO THE TARGET, not to the asker ---------
# Mutant: deliver_wake(..., session_id=from_session).  Every case above read
# only that delivery HAPPENED and what TEXT it carried, never WHERE it went --
# so waking the wrong session (the one that is already awake and asking) was
# deletable green while the row still recorded `delivered`.

def test_delivery_targets_the_session_being_woken(collab, store):
    collab["acquire_mode"]["mode"] = "grant"
    s = store("d1")
    do_wake(FakeRunner(in_flight=False), s)
    assert collab["delivered_calls"], "non-vacuity: delivery must be attempted"
    assert collab["delivered_calls"][0]["session_id"] == "will"
    assert collab["delivered_calls"][0]["session_id"] != "britta", (
        "a wake delivered to the FROM session wakes the asker, not the target"
    )


# --- D2: a gate-2 refusal reason is CARRIED, never re-derived ---------------
# Mutant: reason=str(refusal.reason) replaced with the literal
# "SESSION_NOT_OWNED".  C1 asserts exactly that literal, so a hardcoded reason
# passed -- the module docstring's "verbatim, never re-derived from prose"
# had an arm only for the single reason the test author happened to pick.

@pytest.mark.parametrize("reason", [
    "SESSION_NOT_OWNED",
    "SESSION_COORDINATION_UNAVAILABLE",
    "A_REASON_THIS_SUITE_HAS_NEVER_SEEN",
])
def test_gate2_refusal_reason_is_carried_verbatim(collab, store, monkeypatch, reason):
    def refuse(*, session_id, surface, config, track_liveness):
        return None, FakeRefusal(reason)
    monkeypatch.setattr(WP, "try_acquire_active_session", refuse)
    s = store("d2-" + reason)
    with pytest.raises(WP.WakeRefused):
        do_wake(FakeRunner(), s)
    assert s.all_latest()[0].reason == reason


# --- D3: "the probe said busy" and "the probe broke" are DIFFERENT FACTS ----
# Mutant: gate 3's except-branch relabelled COMPRESSION_IN_FLIGHT.  Verdict,
# row status, lease release and exception type are all identical, so C8 passed.
# What changes is what the operator is told: COMPRESSION_IN_FLIGHT says the
# session is healthy and busy (retry later, it will clear); GATE_ERROR says the
# gate itself could not answer (nothing will clear on its own).

def test_gate3_raising_is_labelled_gate_error_not_compression(collab, store):
    collab["acquire_mode"]["mode"] = "grant"
    s = store("d3")
    with pytest.raises(WP.WakeRefused):
        do_wake(FakeRunner(in_flight=RuntimeError("probe blew up")), s)
    assert s.all_latest()[0].reason == WP.GATE_ERROR


def test_gate3_busy_and_gate3_broken_get_different_reasons(collab, store):
    collab["acquire_mode"]["mode"] = "grant"
    s_busy = store("d3-busy")
    with pytest.raises(WP.WakeRefused):
        do_wake(FakeRunner(in_flight=True), s_busy)
    s_broken = store("d3-broken")
    with pytest.raises(WP.WakeRefused):
        do_wake(FakeRunner(in_flight=RuntimeError("probe blew up")), s_broken)
    busy = s_busy.all_latest()[0].reason
    broken = s_broken.all_latest()[0].reason
    assert busy == WP.COMPRESSION_IN_FLIGHT
    assert broken == WP.GATE_ERROR
    assert busy != broken, "a broken gate must not read as a healthy busy session"


# --- D4: a failing lease release must not mask the wake's outcome -----------
# Mutant: _release() calls release() bare.  The release runs in the `finally`
# of the block that already recorded the outcome, so a raising release
# replaces a DELIVERED return (or a WakeRefused) with the release's own
# exception -- the caller loses the answer the row already holds.

class BadLease:
    def __init__(self, sid):
        self.session_id = sid

    def release(self):
        raise RuntimeError("lease file vanished under us")


@pytest.fixture
def bad_lease(collab, monkeypatch):
    def acquire(*, session_id, surface, config, track_liveness):
        return BadLease(session_id), None
    monkeypatch.setattr(WP, "try_acquire_active_session", acquire)
    return collab


def test_release_failure_does_not_mask_a_successful_wake(bad_lease, store):
    s = store("d4a")
    h = do_wake(FakeRunner(in_flight=False), s)
    assert h.status == DELIVERED
    assert s.all_latest()[0].status == DELIVERED


def test_release_failure_does_not_mask_a_refusal(bad_lease, store):
    s = store("d4b")
    with pytest.raises(WP.WakeRefused):
        do_wake(FakeRunner(in_flight=True), s)
    assert s.all_latest()[0].reason == WP.COMPRESSION_IN_FLIGHT


# --- D5/D6: the row must record WHO asked and IN WHICH DIRECTION ------------
# Mutants: from/to swapped at open_handoff, and requesting_user replaced with
# a constant.  Both survive every case above, because the suite reads only
# status, reason and intent off the row.  The handoff record is the audit
# trail for a wake nobody watched happen; a reversed row names the wrong
# session as woken, and a constant user erases who authorised it.

def test_row_records_the_direction_of_the_wake(collab, store):
    collab["acquire_mode"]["mode"] = "grant"
    s = store("d5")
    do_wake(FakeRunner(in_flight=False), s)
    row = s.all_latest()[0]
    assert (row.from_session, row.to_session) == ("britta", "will")


def test_row_records_the_requesting_user(collab, store):
    collab["acquire_mode"]["mode"] = "grant"
    s = store("d6")
    do_wake(FakeRunner(in_flight=False), s)
    assert s.all_latest()[0].requesting_user == "Britta"


def test_row_direction_matches_the_delivery_target(collab, store):
    """The row and the wire must agree about who was woken."""
    collab["acquire_mode"]["mode"] = "grant"
    s = store("d5b")
    do_wake(FakeRunner(in_flight=False), s)
    assert s.all_latest()[0].to_session == collab["delivered_calls"][0]["session_id"]


# --- case-count floor (rule R14c: set from the MEASURED count, after the run)
# A script/pytest suite that dies halfway prints no failures; this turns a
# truncated collection into a named failing case.
def test_case_count_floor():
    import ast as _ast
    src = open(__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    n = sum(1 for node in _ast.walk(tree)
            if isinstance(node, _ast.FunctionDef) and node.name.startswith("test_"))
    assert n >= 22, f"suite shrank: {n} test functions, floor 22"
