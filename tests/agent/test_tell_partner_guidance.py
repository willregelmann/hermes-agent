"""Agents learn tell-partner, and who their partners are, from the system prompt whenever the tool
is theirs (#52). The capability must not depend on the model reading a tool description.
"""

from types import SimpleNamespace

import pytest
import yaml

from agent.prompt_builder import PEER_WORK_GUIDANCE, TELL_PARTNER_GUIDANCE
from agent.system_prompt import _tool_guidance_block


def _agent(names):
    return SimpleNamespace(valid_tool_names=set(names))


def _partners(home, table):
    (home / "config.yaml").write_text(yaml.safe_dump({"partners": table}), encoding="utf-8")


WILL = {"kind": "human", "primary": {"platform": "telegram", "chat_id": "1"}}
ASH = {"kind": "agent", "peer": "ash"}


def test_guidance_and_roster_present_exactly_when_the_tool_is_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _partners(tmp_path, {"will": WILL, "ash": ASH})

    block = _tool_guidance_block(_agent({"tell_partner"})) or ""
    assert TELL_PARTNER_GUIDANCE in block
    assert "ash (agent)" in block and "will (person)" in block
    assert TELL_PARTNER_GUIDANCE not in (_tool_guidance_block(_agent({"terminal"})) or "")


@pytest.mark.parametrize("table, expected", [
    ({"will": WILL, "ash": ASH}, True),   # an agent among the partners -> the agent-work rules apply
    ({"ash": ASH}, True),
    ({"will": WILL}, False),              # people only: every rule is about agent-to-agent work
    ({}, False),
])
def test_peer_work_guidance_follows_whether_any_partner_is_an_agent(tmp_path, monkeypatch, table, expected):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _partners(tmp_path, table)

    block = _tool_guidance_block(_agent({"tell_partner"})) or ""
    assert (PEER_WORK_GUIDANCE in block) is expected
    assert PEER_WORK_GUIDANCE not in (_tool_guidance_block(_agent({"terminal"})) or "")
