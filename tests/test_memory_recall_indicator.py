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
    """Minimal stand-in: the gate only reads user_config and the source."""

    def __init__(self, cfg, source=""):
        self.user_config = cfg
        self.source = source


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
        user_config = {"display": {"memory_recall_indicator": False}}

    assert _recall_indicator_enabled(Bare()) is False


def test_call_site_is_actually_gated():
    """NON-VACUITY: the original bug was a MISSING condition at the call site.

    A unit test of the helper cannot see that, so assert the wiring directly.
    This fails against pre-fix turn_context.py.

    MATCH THE RIGHT SITE: `if ext_prefetch_cache:` appears twice. Line ~168
    guards INJECTION of the memory block into the prompt and must stay
    ungated — gating it would disable recall itself, not just the indicator.
    The emitter is the one that calls describe_recall(), so anchor on that
    rather than on the first textual match. My first version anchored on the
    first `if ext_prefetch_cache` in the file and failed against correctly
    patched code, which would have read as the fix not working.
    """
    src = (REPO / "agent" / "turn_context.py").read_text()
    idx = src.find("describe_recall()")
    assert idx != -1, "recall indicator emitter not found"
    # The governing condition is the nearest preceding `if ext_prefetch_cache`.
    guard = src.rfind("if ext_prefetch_cache", 0, idx)
    assert guard != -1, "emitter is not guarded by ext_prefetch_cache at all"
    line = src[guard:src.find(":", guard) + 1]
    assert "_recall_indicator_enabled" in line, (
        "recall indicator is emitted unconditionally — the gate is gone"
    )


def test_injection_site_is_NOT_gated():
    """The complement, and the reason the fix is narrow.

    Suppressing the indicator must not suppress the memory itself. If someone
    'tidies' this by gating the injection too, recall silently stops working
    while the config reads as a display preference.
    """
    src = (REPO / "agent" / "turn_context.py").read_text()
    idx = src.find("build_memory_context_block(ext_prefetch_cache)")
    assert idx != -1, "memory injection site not found"
    guard = src.rfind("if ext_prefetch_cache", 0, idx)
    line = src[guard:src.find(":", guard) + 1]
    assert "_recall_indicator_enabled" not in line, (
        "memory INJECTION must not depend on a display setting"
    )


def test_key_is_registered_as_a_display_setting():
    """Unregistered keys silently ignore per-platform overrides."""
    from gateway.display_config import _GLOBAL_DEFAULTS

    assert _GLOBAL_DEFAULTS.get("memory_recall_indicator") is True
