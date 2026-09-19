"""A reply returned to the agent that asked is labelled with the REPLYING agent's name.

Found live on 2026-09-18: Wren handed Ash a request, Ash answered, and the answer arrived in Wren's
``Peer: ash`` session as ``[reply from wren]``. The label used the completion event's ``peer``
field, which is the agent that asked. A woken turn has to be told truthfully where its input came
from, so the label must name this agent (``identity.json``).
"""

import asyncio
import json

import pytest

from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_returned_reply_names_the_agent_that_replied(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "identity.json").write_text(json.dumps({"agent": "ash"}), encoding="utf-8")
    sent = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **kw):
        sent.append(argv)
        return _Proc()

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/hermes")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = object.__new__(GatewayRunner)

    ok = await GatewayRunner._return_peer_completion(
        runner, {"agent": "wren"}, "Handoff received.", {"peer": "wren", "session_id": "s1"})

    assert ok is True
    [argv] = sent
    assert argv[1:4] == ("peer", "dm", "wren")
    assert argv[4].startswith("[reply from ash]")
