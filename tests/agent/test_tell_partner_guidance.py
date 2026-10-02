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


def test_unreadable_roster_warns_instead_of_dropping_the_guidance_silently(tmp_path, monkeypatch, caplog):
    """The gate stays broad so a bad config cannot block session start, but it must not fail
    mute: a rename of load_partners/AGENT would otherwise delete the etiquette forever with
    no error anywhere. Pinning the WARNING is what makes that failure findable."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _partners(tmp_path, {"ash": ASH})

    def _boom():
        raise RuntimeError("partners table unreadable")

    monkeypatch.setattr("hermes_cli.partners.load_partners", _boom)

    with caplog.at_level("WARNING", logger="agent.system_prompt"):
        block = _tool_guidance_block(_agent({"tell_partner"})) or ""

    # Degrades to the tool rules alone -- no roster, no peer etiquette, and session start survives.
    assert TELL_PARTNER_GUIDANCE in block
    assert PEER_WORK_GUIDANCE not in block
    assert "ash (agent)" not in block
    assert any("peer etiquette omitted" in r.getMessage() for r in caplog.records), \
        f"no WARNING emitted; records={[r.getMessage() for r in caplog.records]}"
