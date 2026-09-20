"""The opening of a dream: a scene written by a small model from a random sample of the agent's life.

Entropy matters more than quality here — the same seed every night would make the same dream, and
the point of dreaming is what the agent does with material it did not choose. The sample is drawn
from what the agent actually has: its memories, the conversations it has been having, the skills it
carries, its own earlier dreams, and the hour.

The scene is written in second person and never frames itself as a message from a partner: a dream
may CONTAIN Britta, but a fabricated line attributed to a real person is a different and worse
thing than a strange room.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

# Written the hard way, from the first real dream (2026-09-19): a surreal second-person scene
# reads to the agent like a stranger talking in riddles, and its trained answer is to decline and
# ask what is actually wanted. Nothing happens and there is no dream. The opening therefore states
# a SITUATION in the agent's own idiom — the kind of thing it would start working on — and the
# strangeness is left to the world's answers, which is where dreams keep theirs anyway.
SEED_INSTRUCTIONS = (
    "You write the situation an agent finds itself in the middle of. You are given fragments from "
    "its life: things it remembers, conversations it has been having, things it knows how to do.\n"
    "- 1 to 3 plain sentences. State what is happening and what seems to be wrong.\n"
    "- Ordinary on the surface, in the flat register of an incident note, not a story. Something "
    "is already underway and unexplained; the details are almost-right rather than impossible.\n"
    "- Draw on one or two of the fragments, transformed. Never list them.\n"
    "- No named person speaks or sends anything. No questions, no meta, no mention of dreams.\n"
    "Output only the situation."
)

#: How many fragments of each kind go into the sample.
SAMPLE = {"memories": 4, "conversations": 4, "skills": 3, "dreams": 1}


def gather_fragments(rng: random.Random = None) -> List[str]:
    """A random handful of the agent's own material. Every source is optional."""
    rng = rng or random.Random()
    out: List[str] = []
    for label, items in (
        ("remembers", _memory_lines()), ("has been talking about", _session_titles()),
        ("knows how to", _skill_names()), ("dreamt before", _past_dream_lines()),
    ):
        picks = rng.sample(items, min(len(items), SAMPLE[_KIND[label]])) if items else []
        out += [f"{label}: {p}" for p in picks]
    rng.shuffle(out)
    return out


_KIND = {"remembers": "memories", "has been talking about": "conversations",
         "knows how to": "skills", "dreamt before": "dreams"}


def generate_seed(fragments: List[str] = None) -> str:
    """The scene the dream opens on. Falls back to a fragment-free scene if the model is unavailable."""
    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    fragments = gather_fragments() if fragments is None else fragments
    hour = datetime.now().strftime("%H:%M on a %A")
    user = "Fragments:\n" + ("\n".join(f"- {f}" for f in fragments) or "- (nothing to draw on)") + \
           f"\n\nIt is {hour}. Write the opening."
    try:
        response = call_llm(
            task="dream", temperature=1.0, max_tokens=400,
            messages=[{"role": "system", "content": SEED_INSTRUCTIONS}, {"role": "user", "content": user}])
        scene = (extract_content_or_reasoning(response) or "").strip()
    except Exception as exc:
        logger.debug("dream seed generation failed: %s", exc)
        scene = ""
    return scene or "You are somewhere you know well, in the dark, and something has just moved."


def _memory_lines() -> List[str]:
    from hermes_constants import get_hermes_home

    lines: List[str] = []
    for name in ("MEMORY.md", "USER.md"):
        path = Path(get_hermes_home()) / "memories" / name
        lines += [ln.strip(" -*\t") for ln in _read_lines(path) if len(ln.strip(" -*\t")) > 25]
    return lines


def _session_titles() -> List[str]:
    """Titles of recent conversations, from the same store /goal and /loop read."""
    try:
        from hermes_cli.goals import _get_session_db
        db = _get_session_db()
        rows = db.list_recent_sessions_bounded(limit=40) if db is not None else []
    except Exception as exc:
        logger.debug("dream seed: sessions unavailable: %s", exc)
        return []
    titles = []
    for row in rows or []:
        title = str((row.get("title") if isinstance(row, dict) else "") or "").strip()
        if len(title) > 8:
            titles.append(title)
    return titles


def _skill_names() -> List[str]:
    from hermes_constants import get_hermes_home

    root = Path(get_hermes_home()) / "skills"
    return [p.name.replace("-", " ") for p in root.glob("*/") if p.is_dir()][:200] if root.exists() else []


def _past_dream_lines() -> List[str]:
    from hermes_cli.dreams import list_dreams

    return [str(row.get("seed") or "") for row in list_dreams(limit=8) if row.get("seed")]


def _read_lines(path: Path) -> List[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
