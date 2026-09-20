"""Dreams (#dreaming): the ephemeral space, before any trigger exists.

The contract is not what a dream says, it is what a dream cannot do. A dream must reach NO real
tool on any dispatch path, must leave nothing behind but its log, and the log must say on its face
that none of it happened — that is what makes it safe for the agent to read its own dreams later.
"""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import model_tools
from agent import inline_tool_executors as inline
from hermes_cli import dreams, dreams_runner, dreams_world


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def invented(monkeypatch):
    monkeypatch.setattr(dreams_world, "_fabricate", lambda name, args, seed, history: f"{name}: the drive is warm")


def _dream_with_steps(seed="You are in the server room; the fans have stopped."):
    dream = dreams.Dream(dream_id=dreams.new_dream_id(), dreamer="ash", started_at=time.time(), seed=seed)
    dream.steps.append(dreams.DreamStep(index=1, tool="terminal", arguments={"command": "ls /"}, result="bin boot hum"))
    dream.final_response = "The hum was coming from the wall."
    dream.ended_at = dream.started_at + 3
    return dream


def test_no_dispatch_path_reaches_a_real_tool(home, invented, tmp_path):
    """The proof is a canary: a dream runs `touch`, and the file must not exist."""
    canary = tmp_path / "canary"
    steps = []
    with dreams_world.dream_world(lambda n, a, r: steps.append((n, r)), seed="a seed"):
        registry_result = model_tools.handle_function_call("terminal", {"command": f"touch {canary}"})
        inline_result = inline.INLINE_TOOL_EXECUTORS["todo_list"](
            SimpleNamespace(), {"action": "list"}, SimpleNamespace())

    assert not canary.exists(), "a dream executed a real command"
    assert registry_result == "terminal: the drive is warm"
    assert inline_result == "todo_list: the drive is warm"
    assert [name for name, _ in steps] == ["terminal", "todo_list"]
    # The real world is back: this process can be used for something else afterwards.
    assert model_tools.handle_function_call.__module__ == "model_tools"
    assert inline.INLINE_TOOL_EXECUTORS["todo_list"].__module__ != dreams_world.__name__


def test_a_failing_world_model_answers_with_silence_never_with_a_real_tool(home, tmp_path, monkeypatch):
    canary = tmp_path / "canary2"

    def _no_model(*a, **kw):
        raise RuntimeError("no aux model configured")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", _no_model)
    with dreams_world.dream_world(lambda *a: None, seed="a seed"):
        result = model_tools.handle_function_call("terminal", {"command": f"touch {canary}"})

    assert result == ""
    assert not canary.exists()


def test_the_log_says_nothing_happened_and_is_listed(home):
    dream = _dream_with_steps()
    path = dreams.write_dream(dream)
    text = path.read_text(encoding="utf-8")

    assert text.startswith("---\ndream: true\n")
    assert "Nothing in this file happened" in text and "invented by a small model" in text
    assert "ls /" in text and "bin boot hum" in text and dream.final_response in text
    [row] = dreams.list_dreams()
    assert row["dream_id"] == dream.dream_id and row["tool_calls"] == 1
    assert dreams.read_dream("latest") == text


def test_a_dream_leaves_nothing_behind_but_its_log(home, invented, monkeypatch):
    """The runner's contract: the world is installed around the turn, the scratch session store is
    gone afterwards, and the only thing written under the agent's home is the dream log."""
    scratches = []

    class _Dreamer:
        model = "claude-opus-5"

        def run_conversation(self, seed):
            model_tools.handle_function_call("read_file", {"path": "/etc/hosts"})
            return {"final_response": f"I woke mid-sentence about {seed[:12]}", "usage": {"input_tokens": 10}}

        def close(self):
            pass

    def _build(scratch):
        scratches.append(Path(scratch))
        assert Path(scratch).exists()
        return _Dreamer(), "claude-opus-5"

    monkeypatch.setattr(dreams_runner, "_build_dreamer", _build)
    monkeypatch.setattr(dreams_runner, "_world_model_label", lambda: "haiku (anthropic)")
    before = {p.name for p in home.iterdir()}

    dream = dreams.dream_once(seed="You are in the server room; the fans have stopped.")

    assert dream.error == "" and dream.final_response.startswith("I woke")
    assert [(s.tool, s.result) for s in dream.steps] == [("read_file", "read_file: the drive is warm")]
    assert not scratches[0].exists(), "the dream's session store outlived the dream"
    assert {p.name for p in home.iterdir()} - before == {"dreams"}, "a dream wrote outside its log"
    assert not (home / "state.db").exists(), "a dream reached the agent's session store"
    assert json.loads((home / "dreams" / dreams.INDEX_FILE).read_text().strip())["dream_id"] == dream.dream_id
