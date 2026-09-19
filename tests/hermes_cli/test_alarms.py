"""Self-wake alarm store contracts (#53): one-shot parsing, fire-once claims, and /new cancellation.

Run against a real SessionDB in a temp HERMES_HOME: the claim is a compare-and-set on state_meta
and the cancellation happens inside SessionDB's end stamp, so mocks would hide exactly what matters.
"""

import time

import pytest

from hermes_cli import alarms, goals


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    db = goals._get_session_db()
    yield db
    goals._DB_CACHE.clear()


def test_an_alarm_fires_once_and_a_failed_start_leaves_it_due(store):
    a = alarms.set_alarm("s1", "in 1h", "check the deploy")
    later = time.time() + 3601
    assert [x.alarm_id for x in alarms.due_alarms_for_session("s1", later)] == [a.alarm_id]
    first = alarms.claim_alarm(a, later)
    assert first is not None and first.status == alarms.FIRED
    assert alarms.claim_alarm(a, later) is None, "a second holder of the session must not fire it again"
    assert alarms.due_alarms_for_session("s1", later) == []
    # A wake turn that could not start hands the alarm back.
    assert alarms.release_alarm(first) is not None
    assert [x.alarm_id for x in alarms.due_alarms_for_session("s1", later)] == [a.alarm_id]


def test_recurring_or_unreadable_when_is_refused_and_at_is_the_next_occurrence(store):
    for when in ("every 1h", "30m", "sometime soon", ""):
        with pytest.raises(alarms.AlarmError):
            alarms.set_alarm("s1", when, "x")
    due = alarms.parse_when("at 17:00")
    assert time.time() < due <= time.time() + 24 * 3600
    assert alarms.list_alarms("s1") == []


def test_only_new_and_reset_cancel_pending_alarms(store):
    for sid in ("kept", "new", "reset"):
        store.create_session(session_id=sid, source="cli")
        alarms.set_alarm(sid, "in 1h", f"note for {sid}")
    # Automatic ends (the runtime went away) leave the conversation's alarms pending.
    store.end_session("kept", "agent_close")
    store.end_session("new", "new_session")
    store.promote_to_session_reset("reset", "session_reset")
    assert len(alarms.list_alarms("kept")) == 1
    assert alarms.list_alarms("new") == [] and alarms.list_alarms("reset") == []
    cancelled = [a for a in alarms.list_alarms("new", include_done=True)]
    assert [a.status for a in cancelled] == [alarms.CANCELLED], "cancelled rows stay auditable"
