"""CLI driver for self-wake alarms (#53): the idle tick wakes the session it holds; gateway-routed
alarms are left to the gateway."""

import queue
import time
from types import SimpleNamespace

import pytest

from hermes_cli import alarms, goals
from hermes_cli.cli_loops_mixin import CLILoopsMixin


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield h
    goals._DB_CACHE.clear()


def _due(session_id, route=None, note="check the build"):
    a = alarms.set_alarm(session_id, "in 1h", note, route=route)
    a.due_at = time.time() - 1
    alarms._get_session_db().set_meta(a.key, a.to_json())
    return a


def test_cli_idle_tick_queues_its_own_alarm_once_and_skips_routed_ones(home):
    mine = _due("cli-1")
    _due("cli-1", route={"platform": "telegram", "chat_id": "c"}, note="gateway's")
    cli = SimpleNamespace(session_id="cli-1", _pending_input=queue.Queue())
    CLILoopsMixin._maybe_fire_alarm(cli)
    cli._last_alarm_check = 0.0
    CLILoopsMixin._maybe_fire_alarm(cli)
    queued = [cli._pending_input.get_nowait() for _ in range(cli._pending_input.qsize())]
    assert len(queued) == 1 and mine.alarm_id in queued[0] and "check the build" in queued[0]

