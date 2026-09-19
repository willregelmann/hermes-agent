"""Gateway self-wake firing (#53): an alarm wakes the session it was set in, once, only when idle.

Drives the real ``_alarm_fire_one`` against a real alarm store; the runner's routing surface
(adapter lookup, session key, lane -> session) is stubbed to one chat.
"""

import time
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import alarms, goals

ROUTE = {"platform": "telegram", "chat_id": "c1", "chat_type": "dm"}


class _PushAdapter:
    supports_async_delivery = True

    def __init__(self):
        self.events = []

    async def handle_message(self, event):
        self.events.append(event)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield h
    goals._DB_CACHE.clear()


def _runner(adapter, *, lane_session="s1", busy=False):
    runner = object.__new__(GatewayRunner)
    runner._adapters_for_profile = lambda profile: {Platform.TELEGRAM: adapter}
    runner._build_process_event_source = lambda evt: SessionSource(
        platform=Platform.TELEGRAM, chat_id=evt["chat_id"], chat_type=evt["chat_type"])
    runner._session_key_for_source = lambda source: "agent:main:telegram:dm:c1"
    runner._running_agents = {"agent:main:telegram:dm:c1": object()} if busy else {}
    runner.session_store = SimpleNamespace(peek_session_id=lambda key: lane_session)
    return runner


def _due(note="check the deploy"):
    a = alarms.set_alarm("s1", "in 1h", note, route=ROUTE)
    a.due_at = time.time() - 1  # due now, without waiting an hour
    alarms._get_session_db().set_meta(a.key, a.to_json())
    return a


@pytest.mark.asyncio
async def test_due_alarm_wakes_its_chat_once_as_an_internal_turn(home):
    adapter = _PushAdapter()
    runner = _runner(adapter)
    alarm = _due()
    assert await GatewayRunner._alarm_fire_one(runner, alarm) is True
    assert await GatewayRunner._alarm_fire_one(runner, alarm) is False
    assert len(adapter.events) == 1
    event = adapter.events[0]
    assert event.internal is True and "check the deploy" in event.text and alarm.alarm_id in event.text


@pytest.mark.asyncio
async def test_busy_session_or_moved_chat_leaves_the_alarm_pending(home):
    adapter = _PushAdapter()
    alarm = _due()
    assert await GatewayRunner._alarm_fire_one(_runner(adapter, busy=True), alarm) is False
    # The chat now routes to another session: an alarm fires only where it was set.
    assert await GatewayRunner._alarm_fire_one(_runner(adapter, lane_session="s2"), alarm) is False
    assert adapter.events == []
    assert [a.alarm_id for a in alarms.due_alarms_for_session("s1")] == [alarm.alarm_id]
