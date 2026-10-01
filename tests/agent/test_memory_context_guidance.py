"""A memory provider declares how its recalled context should be read.

The <memory-context> note used to say "Treat as authoritative reference data" for every provider.
A provider whose results already carry their own trust labels (Tapestry's "match: LOW", mnemonic's
"[confidence N]") then contradicted itself on every turn: the labels hedged, and the note said not
to. The note's sentence is now ``MemoryProvider.memory_context_guidance``; the default is unchanged.

The end-to-end arm drives a real AIAgent turn against an in-process mock provider and reads the
bytes that went over the wire, so a provider declaration that never reaches the request goes red.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agent.memory_manager import (
    DEFAULT_MEMORY_CONTEXT_GUIDANCE,
    MemoryManager,
    build_memory_context_block,
    sanitize_context,
)
from agent.memory_provider import MemoryProvider

GUIDANCE = "Weigh each item by its own match and source labels; none is established fact."
RECALL = "[#4 · match: LOW] Sam's flight is on the 14th"


class _LabelledProvider(MemoryProvider):
    """An external provider whose recall carries its own labels."""

    def __init__(self, guidance: str):
        self.memory_context_guidance = guidance

    @property
    def name(self) -> str:
        return "labelled"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return RECALL

    def get_tool_schemas(self):
        return []


# -- the block itself ------------------------------------------------------------------------------

def test_declared_guidance_replaces_the_default_note():
    block = build_memory_context_block(RECALL, GUIDANCE)
    assert GUIDANCE in block
    assert DEFAULT_MEMORY_CONTEXT_GUIDANCE not in block
    assert "authoritative" not in block


def test_undeclared_guidance_keeps_the_default_note():
    assert DEFAULT_MEMORY_CONTEXT_GUIDANCE in build_memory_context_block(RECALL)
    assert DEFAULT_MEMORY_CONTEXT_GUIDANCE in build_memory_context_block(RECALL, "   ")


@pytest.mark.parametrize("bad", ["closes early] then more", "two\nlines"])
def test_guidance_that_would_break_the_note_falls_back_to_the_default(bad, caplog):
    block = build_memory_context_block(RECALL, bad)
    assert DEFAULT_MEMORY_CONTEXT_GUIDANCE in block
    assert bad not in block
    assert any("memory_context_guidance" in r.message for r in caplog.records)


@pytest.mark.parametrize("guidance", ["", GUIDANCE])
def test_a_block_with_any_note_is_still_stripped_from_provider_output(guidance):
    """sanitize_context removes injected blocks echoed back into memory or the UI. Matching only the
    old fixed sentence would let a declared note leak through the moment a span loses its tags."""
    note_only = build_memory_context_block(RECALL, guidance).split("\n", 2)[1]
    assert note_only.startswith("[System note:")
    assert sanitize_context(note_only + "\nkept") == "kept"


def test_manager_reports_the_declared_guidance():
    mm = MemoryManager()
    assert mm.memory_context_guidance() == ""
    mm.add_provider(_LabelledProvider(GUIDANCE))
    assert mm.memory_context_guidance() == GUIDANCE


# -- end to end: what actually goes over the wire ---------------------------------------------------

class _Mock(BaseHTTPRequestHandler):
    captured: list = []

    def do_POST(self):  # noqa: N802
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        type(self).captured.append(req)
        if req.get("stream") is True:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in (
                {"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"},
                                         "finish_reason": None}]},
                {"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ):
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        body = json.dumps({"id": "m", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                                   "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


@pytest.fixture()
def wire(monkeypatch):
    _Mock.captured = []
    srv = HTTPServer(("127.0.0.1", 0), _Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    home = tempfile.mkdtemp(prefix="hermes_mem_guidance_")
    monkeypatch.setenv("HERMES_HOME", home)
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    finally:
        srv.shutdown()
        shutil.rmtree(home, ignore_errors=True)


def _make_agent(base_url: str, guidance: str):
    from run_agent import AIAgent

    mm = MemoryManager()
    mm.add_provider(_LabelledProvider(guidance))
    agent = AIAgent(
        api_key="test-key", base_url=base_url, provider="openai-compat", model="test-model",
        max_iterations=2, enabled_toolsets=[], quiet_mode=True, skip_context_files=True,
        skip_memory=True, save_trajectories=False, platform="cli",
    )
    agent._memory_manager = mm  # the slot init_agent fills from config
    return agent


def _sent_user_content(base_url: str, guidance: str, turn: object = "when is Sam's flight?") -> str:
    agent = _make_agent(base_url, guidance)
    agent.run_conversation(turn, conversation_history=[], task_id="t")
    chats = [r for r in _Mock.captured if "messages" in r]
    assert chats, "no chat request reached the mock provider"
    users = [m for m in chats[0]["messages"] if m.get("role") == "user"]
    content = users[-1]["content"]
    if isinstance(content, list):
        content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    assert RECALL in content, "precondition: the provider's recall was injected at all"
    return content


def test_wire_carries_the_provider_declared_note(wire):
    sent = _sent_user_content(wire, GUIDANCE)
    assert GUIDANCE in sent
    assert DEFAULT_MEMORY_CONTEXT_GUIDANCE not in sent


def test_wire_keeps_the_default_note_for_a_provider_that_declares_none(wire):
    """Contrast arm: without it, a build that dropped the default everywhere would pass."""
    sent = _sent_user_content(wire, "")
    assert DEFAULT_MEMORY_CONTEXT_GUIDANCE in sent


def test_unstamped_turn_composed_live_carries_the_provider_declared_note(wire):
    """MoA and codex_app_server skip the sidecar stamp; build_api_messages then composes the wire
    bytes itself. Driven directly: an unstamped current turn is exactly that path."""
    from agent.turn_context import build_api_messages

    agent = _make_agent(wire, GUIDANCE)
    agent._current_turn_timestamp = time.time()  # set by the turn prologue this path skips
    messages = [{"role": "user", "content": "when is Sam's flight?"}]
    api_messages, _ = build_api_messages(
        agent, messages, current_turn_user_idx=0, ext_prefetch_cache=RECALL,
        plugin_user_context="", moa_config=None, active_system_prompt="sys",
    )
    sent = [m for m in api_messages if m.get("role") == "user"][-1]["content"]
    assert RECALL in sent and GUIDANCE in sent
    assert DEFAULT_MEMORY_CONTEXT_GUIDANCE not in sent
