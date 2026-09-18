"""Behaviour contract for the memory recall indicator gate.

The "🧠 <provider> — recalled N memories" line is emitted unconditionally in
pre-fix code. These assert the CONTRACT (default on, suppressible globally,
suppressible per-platform, CLI unaffected by a chat-only override), not the
current value of any literal.

Non-vacuity: test_pre_fix_code_has_no_gate reads the source and fails if the
call site ever loses its gate again — the defect was an ABSENT condition, and
an absent condition is invisible to a behavioural mock that already has one.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent.turn_context import _recall_indicator_enabled

REPO = Path(__file__).resolve().parents[1]


class _Agent:
    """Minimal stand-in: the gate reads user_config and agent.platform.

    `platform` (not `source`) is the attribute agent_init.py actually sets,
    and it already holds the config key ("cli", "google_chat", ...).
    """

    def __init__(self, cfg, platform=""):
        self.user_config = cfg
        self.platform = platform


def test_default_is_on_when_unconfigured():
    # An unconfigured user must keep today's behaviour.
    assert _recall_indicator_enabled(_Agent({})) is True
    assert _recall_indicator_enabled(_Agent({"display": {}})) is True


def test_global_false_suppresses():
    cfg = {"display": {"memory_recall_indicator": False}}
    assert _recall_indicator_enabled(_Agent(cfg, "google_chat")) is False


@pytest.mark.parametrize("word", ["off", "false", "no", "0", "OFF", " Off "])
def test_string_forms_accepted(word):
    # `hermes config set` writes strings; YAML may quote them.
    cfg = {"display": {"memory_recall_indicator": word}}
    assert _recall_indicator_enabled(_Agent(cfg, "cli")) is False


@pytest.mark.parametrize("word", ["on", "true", "yes", "1"])
def test_truthy_strings_keep_it_on(word):
    cfg = {"display": {"memory_recall_indicator": word}}
    assert _recall_indicator_enabled(_Agent(cfg, "cli")) is True


def test_per_platform_override_scopes_to_one_platform():
    """The whole point: quiet in chat, verbose where I actually debug."""
    cfg = {
        "display": {
            "platforms": {"google_chat": {"memory_recall_indicator": False}}
        }
    }
    assert _recall_indicator_enabled(_Agent(cfg, "google_chat")) is False
    assert _recall_indicator_enabled(_Agent(cfg, "cli")) is True
    assert _recall_indicator_enabled(_Agent(cfg, "api_server")) is True


def test_per_platform_beats_global():
    cfg = {
        "display": {
            "memory_recall_indicator": False,
            "platforms": {"cli": {"memory_recall_indicator": True}},
        }
    }
    assert _recall_indicator_enabled(_Agent(cfg, "cli")) is True
    assert _recall_indicator_enabled(_Agent(cfg, "google_chat")) is False


def test_broken_config_fails_open():
    """A lookup that explodes must not silently disable a signal."""

    class Exploding(dict):
        def get(self, *a, **k):
            raise RuntimeError("boom")

    assert _recall_indicator_enabled(_Agent(Exploding())) is True
    assert _recall_indicator_enabled(_Agent(None)) is True


def test_missing_source_attribute_is_safe():
    class Bare:
        platform = "cli"
        user_config = {"display": {"memory_recall_indicator": False}}

    assert _recall_indicator_enabled(Bare()) is False


def _guard_line_for(src: str, marker: str) -> str:
    """Return the `if ext_prefetch_cache...` line governing `marker`.

    ANCHORING DISCIPLINE (Wren, review of PR #6, generalising her lesson 42):
    an anchor must be unique AND PROVE its uniqueness. My first version used
    `re.search` on a substring and silently took the first of two matches —
    it failed against correctly-patched code, which reads exactly like "the
    fix doesn't work". Switching to `rfind` only picked the other end of the
    same ambiguity.

    So: split into lines, match whole stripped lines, and assert exactly one
    candidate governs the marker. Ambiguity becomes a loud test error instead
    of a silently wrong anchor.
    """
    lines = src.splitlines()
    marker_idx = next(
        (i for i, ln in enumerate(lines) if marker in ln), None
    )
    assert marker_idx is not None, f"marker not found: {marker!r}"
    # An inline conditional (``x = f(ext_prefetch_cache) if ext_prefetch_cache else ""``) is its
    # own guard line.
    if " if ext_prefetch_cache" in lines[marker_idx]:
        return lines[marker_idx].strip()
    candidates = [
        i for i, ln in enumerate(lines)
        if i < marker_idx and ln.strip().startswith("if ext_prefetch_cache")
    ]
    assert candidates, f"no ext_prefetch_cache guard precedes {marker!r}"
    return lines[candidates[-1]].strip()


def test_call_site_is_actually_gated():
    """NON-VACUITY: the original bug was a MISSING condition at the call site.

    A unit test of the helper cannot see that, so assert the wiring directly.
    This fails against pre-fix turn_context.py.
    """
    src = (REPO / "agent" / "turn_context.py").read_text()
    guard = _guard_line_for(src, "describe_recall()")
    assert "_recall_indicator_enabled" in guard, (
        f"recall indicator is emitted unconditionally — gate gone: {guard!r}"
    )


def test_injection_site_is_NOT_gated():
    """The complement, and the reason the fix is narrow.

    Suppressing the indicator must not suppress the memory itself. If someone
    'tidies' this by gating the injection too, recall silently stops working
    while the config reads as a display preference.
    """
    src = (REPO / "agent" / "turn_context.py").read_text()
    guard = _guard_line_for(src, "build_memory_context_block(ext_prefetch_cache)")
    assert "_recall_indicator_enabled" not in guard, (
        f"memory INJECTION must not depend on a display setting: {guard!r}"
    )


def test_the_two_sites_are_distinct():
    """Wren's attack: is test_injection_site_is_NOT_gated load-bearing?

    It only protects anything if the two guards are genuinely separate lines.
    Were they ever merged into one, both tests above would read the SAME line
    and the pair would be self-contradictory rather than protective — so pin
    the structural fact they both depend on.
    """
    src = (REPO / "agent" / "turn_context.py").read_text()
    emit = _guard_line_for(src, "describe_recall()")
    inject = _guard_line_for(src, "build_memory_context_block(ext_prefetch_cache)")
    assert emit != inject, (
        "indicator and injection share one guard — gating the indicator would "
        "also disable memory injection"
    )


def test_platform_key_reads_the_attribute_that_exists():
    """Wren's point 4: `agent.source` is never set on an agent object.

    Reading it first made the branch dead, and a SessionSource landing there
    would silently break per-platform resolution while global still worked.
    """
    src = (REPO / "agent" / "turn_context.py").read_text()
    start = src.find("def _recall_indicator_enabled")
    end = src.find("\ndef ", start + 1)
    body = src[start:end]
    assert 'getattr(agent, "platform"' in body
    assert 'getattr(agent, "source"' not in body, (
        "agent.source is not an attribute of an agent; reading it silently "
        "breaks per-platform resolution"
    )


def test_key_is_registered_as_a_display_setting():
    """Unregistered keys silently ignore per-platform overrides."""
    from gateway.display_config import _GLOBAL_DEFAULTS

    assert _GLOBAL_DEFAULTS.get("memory_recall_indicator") is True
