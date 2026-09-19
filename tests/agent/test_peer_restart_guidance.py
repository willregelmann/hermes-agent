"""Agents learn peer gateway restart from the system prompt whenever the tool is theirs (#54).

The capability must not depend on the model reading a tool description, and must not be taught to
an agent that can't use it.
"""

from types import SimpleNamespace

from agent.prompt_builder import PEER_RESTART_GUIDANCE
from agent.system_prompt import _tool_guidance_block


def _agent(names):
    return SimpleNamespace(valid_tool_names=set(names))


def test_guidance_present_exactly_when_the_tool_is_loaded():
    assert PEER_RESTART_GUIDANCE in (_tool_guidance_block(_agent({"restart_peer_gateway"})) or "")
    assert PEER_RESTART_GUIDANCE not in (_tool_guidance_block(_agent({"terminal"})) or "")
