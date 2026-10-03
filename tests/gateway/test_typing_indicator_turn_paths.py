"""typing_indicator=false must silence EVERY typing call a real turn makes, not only the refresh loop.

Drives a full ``GatewayRunner._run_agent`` turn with tool progress through a recording adapter, so
the turn-start call (run_turn.py) and the post-progress restore (run_turn_runner.py) both execute.
The on arm is the contrast: the same turn with the flag on must type at least once, so the off
arm's zero cannot come from a turn that never reached the typing code.
"""

import importlib
import sys
import time
import types

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource


class TypingRecorder(BasePlatformAdapter):
    def __init__(self, typing_indicator: bool):
        super().__init__(PlatformConfig(enabled=True, token="***", typing_indicator=typing_indicator), Platform.SLACK)
        self.typing_calls = 0
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(content)
        return SendResult(success=True, message_id=f"m-{len(self.sent)}")

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        self.typing_calls += 1

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class ToolUsingAgent:
    """Emits tool progress so the progress path (and its typing restore) runs."""

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        if cb is not None:
            cb("tool.started", "terminal", "pwd", {})
            time.sleep(0.5)
            cb("tool.started", "terminal", "ls", {})
            time.sleep(0.5)
        return {"final_response": "done", "messages": [], "api_calls": 1}


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = types.SimpleNamespace(loaded_hooks=False)
    runner.config = types.SimpleNamespace(
        thread_sessions_per_user=False, group_sessions_per_user=False, stt_enabled=False,
    )
    return runner


def _install_fakes(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = ToolUsingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401 — register terminal emoji
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)


async def _run_turn(adapter):
    runner = _make_runner(adapter)
    source = SessionSource(platform=Platform.SLACK, chat_id="C1", chat_type="dm")
    return await runner._run_agent(
        message="hello", context_prompt="", history=[], source=source,
        session_id="sess-typing-off", session_key="agent:main:slack:dm:C1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_a_real_turn_types_only_when_the_indicator_is_on(monkeypatch, tmp_path, enabled):
    _install_fakes(monkeypatch, tmp_path)
    adapter = TypingRecorder(typing_indicator=enabled)

    result = await _run_turn(adapter)

    assert result["final_response"] == "done"
    assert any("pwd" in s for s in adapter.sent), "precondition: the progress path ran"
    if enabled:
        assert adapter.typing_calls >= 1, "contrast arm: an enabled turn must type"
    else:
        assert adapter.typing_calls == 0
