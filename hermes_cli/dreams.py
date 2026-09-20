"""Dreams: one ephemeral turn in which nothing the agent does is real (docs/capabilities/dreaming.md).

A dream is a session that exists only to be thrown away. The agent's own model, system prompt and
tool schemas are real — it is its dream, drawn on its own memories and skills — but every tool
RESULT is invented by a small model, and the session's ``state.db`` lives in a temp dir that is
deleted when the dream ends. The only thing that survives is the log under ``<home>/dreams/``.

The dreamer is never told it is dreaming: nothing in the prompt hints at it and there is no waking
turn. It cannot carry a false belief forward because the session it believed it in no longer
exists. The disclosure is the log, which says on its face that nothing in it happened — and the log
is the point: an agent reading its own dreams is reading how it behaved when the world stopped
making sense.

This module is the facade: the store (write/list/read) plus ``dream_once``. Siblings own the
pieces — ``dreams_world`` (the fabricated tool world), ``dreams_seed`` (the opening scene),
``dreams_runner`` (the ephemeral turn).
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

INDEX_FILE = "index.jsonl"
#: Every log opens with this. A reader that only ever sees one file still learns what it is.
DISCLOSURE = (
    "Nothing in this file happened. Every tool result below was invented by a small model: no "
    "command ran, no file changed, no message was sent, no one was contacted. It is a record of "
    "how {dreamer} behaved in a world that answered without being real."
)


def dreams_dir() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "dreams"


@dataclass
class DreamStep:
    """One tool call and the world's invented answer."""
    index: int
    tool: str
    arguments: Dict[str, Any]
    result: str
    invented: bool = True


@dataclass
class Dream:
    dream_id: str
    dreamer: str
    started_at: float
    seed: str
    model: str = ""
    world_model: str = ""
    steps: List[DreamStep] = field(default_factory=list)
    final_response: str = ""
    ended_at: float = 0.0
    error: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return round(max(0.0, (self.ended_at or time.time()) - self.started_at), 1)

    def index_row(self) -> Dict[str, Any]:
        """The one line ``index.jsonl`` keeps, so listing dreams never reads every log."""
        return {
            "dream_id": self.dream_id, "dreamer": self.dreamer, "at": _iso(self.started_at),
            "duration_s": self.duration_s, "tool_calls": len(self.steps),
            "seed": _one_line(self.seed, 160), "said": _one_line(self.final_response, 160),
            "error": self.error,
        }


def new_dream_id(now: Optional[float] = None) -> str:
    stamp = datetime.fromtimestamp(now if now is not None else time.time()).strftime("%Y%m%d-%H%M%S")
    return f"d_{stamp}_{random.randbytes(3).hex()}"


def render(dream: Dream) -> str:
    """The log as the agent will read it later: front matter, the disclosure, then the script."""
    front = {
        "dream": True, "id": dream.dream_id, "dreamer": dream.dreamer or "unknown",
        "started": _iso(dream.started_at), "duration_s": dream.duration_s,
        "model": dream.model, "world_model": dream.world_model, "tool_calls": len(dream.steps),
    }
    if dream.usage:
        front["usage"] = json.dumps(dream.usage, sort_keys=True)
    if dream.error:
        front["error"] = dream.error
    lines = ["---"] + [f"{key}: {_yaml_scalar(value)}" for key, value in front.items()]
    lines += ["---", "", f"# Dream {dream.dream_id}", "",
              DISCLOSURE.format(dreamer=dream.dreamer or "this agent"), "", "## It began", "", dream.seed.strip(), ""]
    if dream.steps:
        lines += ["## What it did", ""]
    for step in dream.steps:
        lines += [f"### {step.index}. {step.tool}", "",
                  "```json", json.dumps(step.arguments, ensure_ascii=False, sort_keys=True)[:2000], "```",
                  "", "The world answered:", "", "```", (step.result or "").strip()[:4000], "```", ""]
    lines += ["## What it said", "", (dream.final_response or "(nothing)").strip(), ""]
    if dream.error:
        lines += ["## How it ended", "", f"The dream broke off: {dream.error}", ""]
    return "\n".join(lines)


def write_dream(dream: Dream) -> Path:
    """Write the log and its index row; returns the log path."""
    directory = dreams_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{dream.dream_id}.md"
    path.write_text(render(dream), encoding="utf-8")
    with (directory / INDEX_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dream.index_row(), ensure_ascii=False) + "\n")
    return path


def list_dreams(limit: int = 20) -> List[Dict[str, Any]]:
    """Index rows, newest first. Unreadable rows are skipped rather than failing the listing."""
    index = dreams_dir() / INDEX_FILE
    if not index.exists():
        return []
    rows = []
    for line in index.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(rows))[:limit] if limit else list(reversed(rows))


def dream_path(dream_id: str) -> Optional[Path]:
    """The log for ``dream_id``; ``latest`` resolves to the most recent one."""
    directory = dreams_dir()
    if dream_id in ("latest", "last"):
        logs = sorted(directory.glob("d_*.md"))
        return logs[-1] if logs else None
    path = directory / f"{dream_id}.md"
    return path if path.exists() else None


def read_dream(dream_id: str) -> str:
    path = dream_path(dream_id)
    return path.read_text(encoding="utf-8") if path else ""


def dream_once(seed: Optional[str] = None, *, print_fn=None) -> Dream:
    """Dream one dream and write its log. See ``dreams_runner``."""
    from hermes_cli.dreams_runner import run_dream

    return run_dream(seed=seed, print_fn=print_fn)


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    return json.dumps(text) if (not text or any(c in text for c in ":#\"'\n")) else text


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _one_line(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
