"""Agents learn tell-partner, and who their partners are, from the system prompt whenever the tool
is theirs (#52). The capability must not depend on the model reading a tool description.
"""

from types import SimpleNamespace

import yaml

from agent.prompt_builder import TELL_PARTNER_GUIDANCE
from agent.system_prompt import _tool_guidance_block


def _agent(names):
    return SimpleNamespace(valid_tool_names=set(names))


def test_guidance_and_roster_present_exactly_when_the_tool_is_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"partners": {
        "will": {"kind": "human", "primary": {"platform": "telegram", "chat_id": "1"}},
        "ash": {"kind": "agent", "peer": "ash"},
    }}), encoding="utf-8")

    block = _tool_guidance_block(_agent({"tell_partner"})) or ""
    assert TELL_PARTNER_GUIDANCE in block
    assert "ash (agent)" in block and "will (person)" in block
    assert TELL_PARTNER_GUIDANCE not in (_tool_guidance_block(_agent({"terminal"})) or "")
