"""The ``alarm`` tool (#53): sets an alarm on the CURRENT session, refuses where nothing would wake,
and the system prompt teaches it wherever the tool is loaded."""

import json
from types import SimpleNamespace

import pytest

from agent.delegation_context import delegated_child_context
from agent.prompt_builder import SELF_WAKE_GUIDANCE
from agent.system_prompt import _tool_guidance_block
from hermes_cli import alarms, goals
from tools.alarm_tool import alarm_tool


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield h
    goals._DB_CACHE.clear()


def test_set_list_cancel_act_on_the_current_session(home, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-now")
    made = json.loads(alarm_tool("set", when="in 2h", note="look at CI"))
    assert made["success"] and made["alarm_id"]
    [stored] = alarms.list_alarms("sess-now")
    assert (stored.alarm_id, stored.note, stored.route) == (made["alarm_id"], "look at CI", {})
    listed = json.loads(alarm_tool("list"))["alarms"]
    assert [a["alarm_id"] for a in listed] == [made["alarm_id"]]
    assert json.loads(alarm_tool("cancel", alarm_id=made["alarm_id"]))["success"]
    assert alarms.list_alarms("sess-now") == []


def test_refuses_where_nothing_would_be_woken(home, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-now")
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    assert "error" in json.loads(alarm_tool("set", when="in 1h", note="x"))
    monkeypatch.delenv("HERMES_CRON_SESSION")
    with delegated_child_context("child"):
        assert "error" in json.loads(alarm_tool("set", when="in 1h", note="x"))
    assert alarms.list_alarms("sess-now") == [] and alarms.list_alarms("child") == []


def test_guidance_present_exactly_when_the_tool_is_loaded():
    with_tool = _tool_guidance_block(SimpleNamespace(valid_tool_names={"alarm"})) or ""
    without = _tool_guidance_block(SimpleNamespace(valid_tool_names={"terminal"})) or ""
    assert SELF_WAKE_GUIDANCE in with_tool and SELF_WAKE_GUIDANCE not in without
