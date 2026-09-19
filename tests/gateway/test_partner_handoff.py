"""Tell-partner delivery to a person (#52): the intent wakes the partner's LIVE session in their chat
as an internal turn, and its handoff row closes only when that turn has finished.

Drives the real ``deliver_to_human`` and the gateway's real post-turn hook against a real handoff
store; the runner's routing surface (adapters, routing index) is stubbed to two chats.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.handoff import DEFERRED, DELIVERED, OPEN
from gateway.partner_handoff import deliver_to_human, handoff_store
from gateway.run import GatewayRunner
from gateway.session import SessionSource

WILL = {"platform": "telegram", "chat_id": "will-chat"}
NOW = datetime(2026, 9, 18, 12, 0)


class _PushAdapter:
    supports_async_delivery = True

    def __init__(self, *, accepts=True):
        self.accepts = accepts
        self.events = []

    async def handle_message(self, event):
        self.events.append(event)
        event._gateway_accepted = self.accepts


def _entry(session_id, chat_id, age_min):
    return SimpleNamespace(
        session_id=session_id, session_key=f"agent:main:telegram:dm:{chat_id}",
        updated_at=NOW - timedelta(minutes=age_min),
        origin=SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm"))


def _runner(adapter, entries):
    runner = object.__new__(GatewayRunner)
    runner._adapters_for_profile = lambda profile: {Platform.TELEGRAM: adapter}
    runner.session_store = SimpleNamespace(list_sessions=lambda: list(entries))
    runner._get_executor = lambda: None
    return runner


async def _tell_will(runner, tmp_path, intent="Britta asked you to say hi"):
    return await deliver_to_human(runner, partner="will", primary=WILL, intent=intent,
                                  from_session="britta-live", requester="britta", profile=None,
                                  home=str(tmp_path))


@pytest.mark.asyncio
async def test_intent_wakes_the_live_session_and_closes_when_that_turn_ends(tmp_path):
    adapter = _PushAdapter()
    # Will's chat rotated (/new): the older session still has a routing row but is not the live one.
    entries = [_entry("will-old", "will-chat", 90), _entry("will-live", "will-chat", 5),
               _entry("britta-live", "britta-chat", 1)]
    runner = _runner(adapter, entries)

    result = await _tell_will(runner, tmp_path)

    assert result.get("success") and result["status"] == "queued", result
    [event] = adapter.events
    assert event.internal is True and event.source.chat_id == "will-chat"
    assert "Britta asked you to say hi" in event.text and "britta" in event.text
    store = handoff_store(str(tmp_path))
    row = store.get(result["handoff_id"])
    assert (row.status, row.to_session, row.from_session) == (OPEN, "will-live", "britta-live")

    # The woken turn finishes: the gateway's post-turn hook closes the row.
    await GatewayRunner._run_post_turn_hooks(runner, agent_result={"final_response": "Hi from Britta!"},
                                             source=event.source, is_internal=True, event=event)
    assert store.get(result["handoff_id"]).status == DELIVERED


@pytest.mark.asyncio
async def test_no_live_session_or_refused_admission_is_an_error_not_a_silent_success(tmp_path):
    # Will has never messaged the agent in that chat: nothing to wake.
    adapter = _PushAdapter()
    result = await _tell_will(_runner(adapter, [_entry("britta-live", "britta-chat", 1)]), tmp_path)
    assert "NO_PRIMARY" in result.get("error", "") and adapter.events == []

    # The adapter did not admit the event: the tool says so and the row is deferred, not open.
    refusing = _PushAdapter(accepts=False)
    result = await _tell_will(_runner(refusing, [_entry("will-live", "will-chat", 5)]), tmp_path)
    assert "error" in result and result.get("handoff_id")
    assert handoff_store(str(tmp_path)).get(result["handoff_id"]).status == DEFERRED
