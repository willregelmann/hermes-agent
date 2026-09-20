"""Running one dream: the agent's real self, one turn, in a session that is thrown away.

What is real: the model, the system prompt (SOUL, skills, identity), the tool schemas, the agent's
own loop. What is not: every tool result (``dreams_world``) and the session itself — the transcript
is written into a temp ``state.db`` that is deleted when the dream ends, so nothing reaches the
agent's store, its memories, or any future turn.

``skip_memory=True`` is load-bearing twice over: the memory manager is the one tool path that does
not go through the two seams the dream world replaces, and a dream must not be able to write a
memory. The cost is that the dream's system prompt lacks the memory block, so it does not share the
prefix cache with the agent's waking sessions.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: A dream ends when the agent stops calling tools. This is the same ceiling a CLI turn has; it is
#: not a limit on dreaming, it is the loop's own bound.
MAX_ITERATIONS = 30


def run_dream(seed: Optional[str] = None, *, print_fn=None) -> Any:
    """Dream once and write the log. Returns the ``Dream``."""
    from hermes_cli.dreams import Dream, DreamStep, new_dream_id, write_dream
    from hermes_cli.dreams_seed import generate_seed
    from hermes_cli.dreams_world import dream_world
    from hermes_cli.partners import own_agent_name

    say = print_fn or (lambda *_a, **_k: None)
    started = time.time()
    seed = (seed or "").strip() or generate_seed()
    dream = Dream(dream_id=new_dream_id(started), dreamer=own_agent_name(), started_at=started, seed=seed)

    def record(name: str, args: Dict[str, Any], result: str) -> None:
        dream.steps.append(DreamStep(index=len(dream.steps) + 1, tool=name, arguments=args, result=result))
        say(f"  · {name}")

    scratch = Path(tempfile.mkdtemp(prefix="hermes-dream-"))
    agent = None
    try:
        with dream_world(record, seed=seed):
            agent, dream.model = _build_dreamer(scratch)
            say(f"  dreaming as {dream.dreamer or 'this agent'} on {dream.model}…")
            result = agent.run_conversation(seed)
        dream.final_response = str((result or {}).get("final_response") or "")
        dream.usage = dict((result or {}).get("usage") or {})
    except Exception as exc:
        dream.error = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning("dream %s broke off: %s", dream.dream_id, dream.error)
    finally:
        _close(agent)
        shutil.rmtree(scratch, ignore_errors=True)
    dream.ended_at = time.time()
    dream.world_model = _world_model_label()
    path = write_dream(dream)
    say(f"  dream written to {path}")
    return dream


def _build_dreamer(scratch: Path) -> Tuple[Any, str]:
    """The agent as it really is, pointed at a throwaway session store."""
    from hermes_state import SessionDB
    from run_agent import AIAgent

    runtime, model = _main_runtime()
    agent = AIAgent(
        model=model or None, provider=runtime.get("provider"), api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"), api_mode=runtime.get("api_mode"),
        credential_pool=runtime.get("credential_pool"),
        request_overrides=dict(runtime.get("request_overrides") or {}),
        session_db=SessionDB(scratch / "state.db"),
        max_iterations=MAX_ITERATIONS, quiet_mode=True, platform="cli",
        skip_memory=True, clarify_callback=None,
    )
    agent._owns_session_db = True
    # A dream spawns nothing: the between-turns MCP refresh would start real MCP servers.
    agent._skip_mcp_refresh = True
    agent.suppress_status_output = True
    agent.stream_delta_callback = None
    return agent, str(getattr(agent, "model", "") or model or "")


def _main_runtime() -> Tuple[Dict[str, Any], str]:
    """The provider/model the agent normally runs on, resolved the way the CLI resolves it."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.runtime_provider import resolve_runtime_provider

    cfg = load_config_readonly() or {}
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    requested = str(model_cfg.get("provider") or "auto")
    model = str(model_cfg.get("default") or model_cfg.get("model") or "")
    runtime = resolve_runtime_provider(requested=requested, target_model=model) or {}
    resolved = runtime.get("model")
    return runtime, (resolved.strip() if isinstance(resolved, str) and resolved.strip() else model)


def _world_model_label() -> str:
    """Which small model answered the dream's tools, for the log's front matter."""
    from hermes_cli.config import load_config_readonly

    aux = ((load_config_readonly() or {}).get("auxiliary") or {}).get("dream") or {}
    provider, model = str(aux.get("provider") or "auto"), str(aux.get("model") or "")
    return f"{model or 'auto'} ({provider})"


def _close(agent: Any) -> None:
    if agent is None:
        return
    try:
        agent.close()
    except Exception as exc:
        logger.debug("dream agent close failed: %s", exc)
