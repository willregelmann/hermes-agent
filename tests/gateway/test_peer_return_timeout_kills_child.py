"""The return-leg timeout must not orphan the ``hermes peer dm`` child (issue #80).

MEASURED BEHAVIOUR THAT MOTIVATES THIS (not a hypothetical): ``asyncio.wait_for``
cancels the AWAIT, not the PROCESS. On timeout the coroutine raises
``TimeoutError``, the ``except`` logs and returns False — and the child keeps
running. A merely-slow ``hermes peer dm`` therefore DELIVERS THE REPLY LATER,
while the gateway has already recorded a failure and requeued. That is
duplicate delivery, not loss, and it is the worse of the two because a drop is
at least consistent.

THE TEST ASSERTS THE PROCESS IS DEAD, NOT THAT THE CALL RETURNED FALSE.
Returning False is what the buggy code already does, so a verdict-only
assertion passes against the orphan. The distinguishing observable is the child
itself: ``os.kill(pid, 0)`` after the call, plus whether the child's
side-effect landed.

PATCH WHERE PRODUCTION READS. ``_return_peer_completion`` imports asyncio and
shutil INSIDE the function body (``import asyncio as _asyncio``), so those
names resolve against the real stdlib modules every call. Patching
``gateway.run_peer_completion._asyncio`` would be a no-op that passes
silently — the module attribute does not exist at module scope. The seam is
the stdlib module object.

SCOPE, STATED HONESTLY: killing the child NARROWS the duplicate-delivery
window, it does not close it. A child killed after the peer accepted the DM
but before the ack was read has still delivered. Distinguishing
"accepted, ack lost" from "never arrived" needs an idempotency key on the
reply, which is a separate issue.
"""

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gateway.run_peer_completion import GatewayPeerCompletionMixin  # noqa: E402


def _pid_alive(pid: int) -> bool:
    """True if the process still exists. ``os.kill(pid, 0)`` does not signal."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _Runner(GatewayPeerCompletionMixin):
    """Minimal host for the mixin: only the return leg is under test."""


@pytest.fixture
def slow_child(tmp_path, monkeypatch):
    """Replace ``hermes peer dm`` with a child that sleeps, then writes a file.

    The marker file is the proof of DELIVERY-AFTER-TIMEOUT: if it appears, the
    orphan went on to do the work the gateway had already given up on.
    """
    marker = tmp_path / "delivered-after-timeout"
    script = tmp_path / "slow-hermes"
    script.write_text("#!/bin/sh\nsleep 3\necho delivered > " + str(marker) + "\n")
    script.chmod(0o755)

    monkeypatch.setattr(shutil, "which", lambda _n: str(script))
    monkeypatch.setattr(
        "hermes_cli.partners.own_agent_name", lambda: "ash", raising=False
    )
    return marker


def _call_with_short_timeout(monkeypatch, timeout_s=0.4):
    """Run the return leg with the production timeout shortened.

    The timeout VALUE is not under test; the cleanup on expiry is. A 120s real
    wait would make this test too slow to be worth running.
    """
    real_wait_for = asyncio.wait_for
    created = []

    async def _short(aw, timeout=None):
        return await real_wait_for(aw, timeout=timeout_s)

    real_exec = asyncio.create_subprocess_exec

    async def _spy_exec(*a, **kw):
        proc = await real_exec(*a, **kw)
        created.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "wait_for", _short)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy_exec)

    runner = _Runner()
    ok = asyncio.run(
        runner._return_peer_completion(
            {"agent": "wren"}, "a reply body", {"peer": "wren"}
        )
    )
    return ok, created


def test_timeout_reports_failure(slow_child, monkeypatch):
    """Baseline: the call must still report failure. Passes before the fix too."""
    ok, created = _call_with_short_timeout(monkeypatch)
    assert created, "fixture did not spawn a child; the test is measuring nothing"
    assert ok is False


def test_timeout_does_not_orphan_the_child(slow_child, monkeypatch):
    """THE RED TEST: the child must be dead when the call returns.

    Against unfixed code the child is alive here, because ``wait_for``
    cancelled the await and nothing touched the process.
    """
    _ok, created = _call_with_short_timeout(monkeypatch)
    assert created, "fixture did not spawn a child; the test is measuring nothing"
    pid = created[0].pid
    assert not _pid_alive(pid), (
        f"child pid {pid} survived the return-leg timeout: the reply can still "
        "be delivered after the gateway recorded failure and requeued (issue #80)"
    )


def test_reap_is_bounded_when_a_grandchild_holds_stdout(tmp_path, monkeypatch):
    """The reap must not hang on a dead child whose pipe is still open.

    Wren's review of this PR (handoff 3e8151e3) asked whether the unbounded
    ``await proc.wait()`` could deadlock, citing the subprocess docs' warning
    about a full pipe buffer. Her measurement said no. Mine said yes, and the
    disagreement was real: her fixture left no survivor holding the pipe.

    asyncio's ``Process.wait()`` completes when the process has exited AND its
    pipes have closed. A grandchild that inherited stdout keeps it open, so
    ``wait()`` never returns even though ``os.kill(pid, 0)`` says the child is
    gone. Measured on the same child shape:

        sh -c "dd ...; sleep 60"        -> never reaps (grandchild holds pipe)
        sh -c "dd ...; exec sleep 60"   -> reaps in 0.001s

    ``hermes peer dm`` is a CLI that may spawn helpers, so this is reachable in
    production. Without a bound the gateway's return leg hangs forever on a
    child it has already killed — strictly worse than the duplicate delivery
    this PR exists to prevent.

    The whole call is bounded here, so an unbounded regression fails this test
    by timing out rather than hanging the suite.
    """
    marker = tmp_path / "never"
    script = tmp_path / "grandchild-hermes"
    # 500KB past the pipe buffer, then a GRANDCHILD inheriting stdout.
    script.write_text(
        "#!/bin/sh\n"
        "dd if=/dev/zero bs=1024 count=500 2>/dev/null\n"
        "sleep 60\n"
        f"echo delivered > {marker}\n"
    )
    script.chmod(0o755)

    monkeypatch.setattr(shutil, "which", lambda _n: str(script))
    monkeypatch.setattr(
        "hermes_cli.partners.own_agent_name", lambda: "ash", raising=False
    )

    real_wait_for = asyncio.wait_for
    created = []
    real_exec = asyncio.create_subprocess_exec

    async def _short(aw, timeout=None):
        # Shorten ONLY the 120s communicate() wait. The reap's own bound is the
        # thing under test and must come from production, not from here.
        return await real_wait_for(aw, timeout=1.0 if timeout == 120 else timeout)

    async def _spy(*a, **kw):
        p = await real_exec(*a, **kw)
        created.append(p)
        return p

    monkeypatch.setattr(asyncio, "wait_for", _short)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy)

    runner = _Runner()

    async def _bounded():
        return await real_wait_for(
            runner._return_peer_completion(
                {"agent": "wren"}, "a reply body", {"peer": "wren"}
            ),
            timeout=20.0,
        )

    started = time.time()
    ok = asyncio.run(_bounded())
    elapsed = time.time() - started

    assert created, "fixture did not spawn a child; the test is measuring nothing"
    assert ok is False
    assert elapsed < 15, (
        f"the return leg took {elapsed:.1f}s: the reap is unbounded and hung on "
        "a dead child whose grandchild still holds stdout"
    )
    assert not _pid_alive(created[0].pid)


def test_orphan_delivers_after_the_gateway_gave_up(slow_child, monkeypatch):
    """THE CONSEQUENCE, measured end to end rather than argued.

    Wait past the child's own sleep. If the marker appears, the reply was
    delivered by a process the gateway had already written off — the
    duplicate-delivery mechanism in issue #80, demonstrated.
    """
    _ok, created = _call_with_short_timeout(monkeypatch)
    assert created, "fixture did not spawn a child; the test is measuring nothing"
    deadline = time.time() + 5
    while time.time() < deadline:
        if slow_child.exists():
            break
        time.sleep(0.1)
    assert not slow_child.exists(), (
        "the orphaned child completed its delivery after the timeout: the peer "
        "receives the reply twice once the requeued attempt also succeeds"
    )
