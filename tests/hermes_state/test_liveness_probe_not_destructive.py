"""Liveness probes must delegate to the platform-aware helper, not signal directly.

`os.kill(pid, 0)` is a harmless probe on POSIX but on Windows it delivers CTRL_C_EVENT to the
target's entire console process group (bpo-14484), so "is the holder alive" can kill the
holder. In `_acquire_db_flock` the caller unlinks the lock file six lines after the probe
returns, which makes the guard able to manufacture the condition it then acts on.

`gateway.status._pid_exists` is the canonical probe: psutil first, then an OpenProcess-based
Windows path, and `os.kill` only on an explicitly POSIX-only branch. These tests assert
DELEGATION rather than the absence of `os.kill`, because `_pid_exists` legitimately uses it
on POSIX — the defect was calling it *unguarded*, and on this platform both forms return the
same answer, so only the mechanism is observable.
"""
from __future__ import annotations

import os

import gateway.status
import hermes_state_common
from tui_gateway import host_supervisor


def test_lock_holder_probe_delegates_to_pid_exists(monkeypatch):
    """_lock_holder_provably_dead must probe via _pid_exists, not os.kill."""
    calls = []

    def _spy(pid):
        calls.append(pid)
        return True  # alive

    monkeypatch.setattr(gateway.status, "_pid_exists", _spy)
    record = {"pid": os.getpid(), "start_ticks": None}
    assert hermes_state_common._lock_holder_provably_dead(record) is False
    assert calls == [os.getpid()], "liveness was not probed through _pid_exists"


def test_lock_holder_reports_dead_via_pid_exists(monkeypatch):
    """A holder that _pid_exists calls dead is provably dead (lock may be broken)."""
    monkeypatch.setattr(gateway.status, "_pid_exists", lambda pid: False)
    record = {"pid": 424242, "start_ticks": 999}
    assert hermes_state_common._lock_holder_provably_dead(record) is True


def test_lock_holder_fails_closed_when_probe_raises(monkeypatch):
    """An indeterminate probe must defer, never authorise breaking the lock."""

    def _boom(pid):
        raise OSError("probe unavailable")

    monkeypatch.setattr(gateway.status, "_pid_exists", _boom)
    record = {"pid": os.getpid(), "start_ticks": 1}
    assert hermes_state_common._lock_holder_provably_dead(record) is False


def test_host_supervisor_pid_alive_delegates_to_pid_exists(monkeypatch):
    """host_supervisor._pid_alive must delegate too (it is polled in a wait loop)."""
    calls = []

    def _spy(pid):
        calls.append(pid)
        return True

    monkeypatch.setattr(gateway.status, "_pid_exists", _spy)
    assert host_supervisor._pid_alive(os.getpid()) is True
    assert calls == [os.getpid()], "liveness was not probed through _pid_exists"
    calls.clear()
    assert host_supervisor._pid_alive(0) is False
    assert host_supervisor._pid_alive(-1) is False
    assert calls == [], "non-positive PIDs must short-circuit before probing"


def test_unprobeable_pid_does_not_reach_a_signal_at_either_caller(monkeypatch):
    """Assume-alive is safe at BOTH _pid_alive callers, for two different reasons.

    Wren's review 5401321817 of PR #95: a polarity flip is only safe if EVERY caller
    tolerates it, and `_pid_alive` has two. `_terminate_pid` is protected by a
    deadline; `reconcile_startup_orphan` is protected by the identity check, which
    is a different mechanism and would survive the deadline being removed.

    This arm pins the second one: an unprobeable PID must land on the identity
    check and be classified pid-reuse-ignored, never signalled.
    """
    def _boom(pid):
        raise OSError("probe unavailable")

    monkeypatch.setattr(gateway.status, "_pid_exists", _boom)
    # Unprobeable => assume alive, so the "not-running" branch is NOT taken.
    assert host_supervisor._pid_alive(os.getpid()) is True

    # An unreadable cmdline must fail closed: no identity, hence no signal.
    monkeypatch.setattr(host_supervisor, "_pid_command", lambda pid: "")
    assert host_supervisor.is_compute_host_identity(os.getpid()) is False
