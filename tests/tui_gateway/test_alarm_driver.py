"""TUI/desktop driver for self-wake alarms (#53): a turn that never starts leaves the alarm due."""

import time

import pytest

from hermes_cli import alarms, goals


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


def test_tui_turn_that_never_starts_leaves_the_alarm_due(home, monkeypatch):
    from tui_gateway import server

    alarm = _due("tui-1")
    session = {"session_key": "tui-1"}
    released = []
    monkeypatch.setattr(server, "_notif_claim_turn", lambda s: True)
    monkeypatch.setattr(server, "_notif_release_turn", lambda s: released.append(True))
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: False)
    server._maybe_fire_tui_alarm("rpc-1", session)
    assert released and [a.alarm_id for a in alarms.due_alarms_for_session("tui-1")] == [alarm.alarm_id]

    submitted = []
    monkeypatch.setattr(server, "_run_prompt_submit", lambda rid, sid, s, text: submitted.append(text) or True)
    server._maybe_fire_tui_alarm("rpc-1", session)
    assert len(submitted) == 1 and alarm.alarm_id in submitted[0]
    assert alarms.due_alarms_for_session("tui-1") == []
