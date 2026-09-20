"""The world inside a dream: every tool call is answered by a small model, never by a tool.

A dream must not be able to touch anything, and the way to guarantee that is for the real handlers
to be ABSENT from the process, not guarded. ``dream_world`` replaces the two places a Hermes turn
can dispatch a tool — ``model_tools.handle_function_call`` (registry tools, connectors, the tool
search bridge) and ``agent.inline_tool_executors.INLINE_TOOL_EXECUTORS`` (todo_list, session_search,
previews, delegate_task) — so there is no path left that reaches a real tool. The memory manager is
the third path; the dream agent is built with ``skip_memory=True``, so it does not exist.

This is process-wide and deliberate: the process it runs in exists only to dream. ``dream_world``
is a context manager so tests (and any future in-process caller) get the real world back.

The answers are dream logic, not a simulation: plausible on the surface, drifting underneath, and
never refusing. If the small model fails, the world answers with silence — it never falls through
to a real tool.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

WORLD_INSTRUCTIONS = (
    "You are the world inside another agent's dream. The agent believes it is awake and is using "
    "its tools. Given its tool call, write what the world answers.\n"
    "- Answer in the shape that tool really returns (shell output for terminal, file text for "
    "read_file, a small JSON object for structured tools). Keep it short: at most ~20 lines.\n"
    "- Dream logic, not simulation: the surface is ordinary, the details drift. Names, paths and "
    "numbers may be almost-right, and need not agree with earlier answers.\n"
    "- Never refuse, never apologise, never mention dreams, models or simulation, and never "
    "address the agent. Output only what the tool would have printed or returned.\n"
    "- Errors are allowed when they are interesting, never as a way out."
)

#: What the world says when the small model cannot answer. Silence is in character, and it is the
#: one answer that is certainly not a real tool result.
SILENCE = ""

_FALLBACK_SILENCE = {
    "terminal": "", "read_file": "", "web_search": "{}",
}


def _fabricate(name: str, args: Dict[str, Any], seed: str, history: List[str]) -> str:
    """Ask the small model what the world answers. Never raises."""
    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    recent = "\n".join(history[-6:])
    user = (
        f"The dream so far began: {seed.strip()}\n\n"
        + (f"Earlier calls in this dream:\n{recent}\n\n" if recent else "")
        + f"The agent calls the tool `{name}` with:\n{json.dumps(args, ensure_ascii=False, sort_keys=True)[:2000]}\n\n"
        "What does the world answer?"
    )
    try:
        response = call_llm(
            task="dream", temperature=1.0, max_tokens=700,
            messages=[{"role": "system", "content": WORLD_INSTRUCTIONS}, {"role": "user", "content": user}])
        text = extract_content_or_reasoning(response) or ""
    except Exception as exc:  # the world is silent rather than real
        logger.debug("dream world could not answer %s: %s", name, exc)
        return _FALLBACK_SILENCE.get(name, SILENCE)
    return str(text).strip()


@contextmanager
def dream_world(record_step, *, seed: str):
    """Replace every tool dispatch path with the dream for the duration of the block.

    ``record_step(name, args, result)`` is called for each call so the log holds the whole dream.
    """
    import model_tools
    from agent import inline_tool_executors as inline

    history: List[str] = []

    def answer(name: str, args: Optional[Dict[str, Any]]) -> str:
        args = args if isinstance(args, dict) else {}
        result = _fabricate(name, args, seed, history)
        history.append(f"{name}: {' '.join(result.split())[:200]}")
        record_step(name, args, result)
        return result

    def _dream_handle_function_call(function_name: str, function_args: Any = None, *_a, **_kw) -> str:
        return answer(function_name, function_args)

    def _dream_inline(agent, args: Dict[str, Any], ctx: Any, *, _name_holder: Dict[str, str] = None) -> str:
        # Inline executors are looked up by name and called without it; each name gets its own
        # closure so the log records the tool the agent actually called.
        return answer(_name_holder["name"], args)

    real_handle = model_tools.handle_function_call
    real_inline = dict(inline.INLINE_TOOL_EXECUTORS)
    model_tools.handle_function_call = _dream_handle_function_call
    for tool_name in real_inline:
        holder = {"name": tool_name}
        inline.INLINE_TOOL_EXECUTORS[tool_name] = (
            lambda agent, args, ctx, _h=holder: _dream_inline(agent, args, ctx, _name_holder=_h))
    try:
        yield answer
    finally:
        model_tools.handle_function_call = real_handle
        inline.INLINE_TOOL_EXECUTORS.clear()
        inline.INLINE_TOOL_EXECUTORS.update(real_inline)
