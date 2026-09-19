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




@pytest.mark.parametrize("reply, forwarded", [("Ash, the deploy is green.", True), ("[SILENT]", False)])
@pytest.mark.asyncio
async def test_alarm_reply_in_an_agent_pair_session_reaches_that_agent(home, monkeypatch, reply, forwarded):
    """A stateless (api_server) session has no chat to push into; in ``Peer: <agent>`` the woken
    turn's words are for that agent, so the turn is told so and they are sent there, labelled with
    who sent them and why (#52). [SILENT] sends nothing. The wake runs under the profile the alarm
    was set in, so a served profile's session resumes in its own store.

    Found live 2026-09-18: without the notice saying where the reply goes, the woken turn also
    used tell_partner, and the peer got the same news twice (once unlabelled)."""
    import asyncio
    import json

    import gateway.wake
    from hermes_cli.subcommands import peer

    (home / "identity.json").write_text(json.dumps({"agent": "wren"}), encoding="utf-8")
    db = goals._get_session_db()
    db.create_session("s1", "api_server")
    db.set_session_title("s1", "Peer: ash")
    woken, sent = [], []

    async def woken_turn(adapter, *, text, session_id, profile=None, **kw):
        woken.append((session_id, profile, text))
        return reply

    monkeypatch.setattr(gateway.wake, "deliver_wake", woken_turn)
    monkeypatch.setattr(peer, "send_to_peer", lambda target, text: sent.append((target, text)))
    runner = object.__new__(GatewayRunner)
    runner._get_executor = lambda: None
    runner._adapters_for_profile = lambda profile: {Platform.API_SERVER: SimpleNamespace(supports_async_delivery=False)}
    alarm = alarms.set_alarm("s1", "in 1h", "tell ash how the deploy went",
                             route={"platform": "api_server", "profile": "house"})
    alarm.due_at = time.time() - 1

    assert await GatewayRunner._alarm_fire_one(runner, alarm) is True
    await asyncio.gather(*runner._alarm_tasks)

    [(session_id, profile, notice)] = woken
    assert (session_id, profile) == ("s1", "house")
    assert "reply is sent to ash" in notice
    if forwarded:
        [(target, text)] = sent
        assert target == "ash" and text.startswith("[from wren") and alarm.alarm_id in text and reply in text
    else:
        assert sent == []
