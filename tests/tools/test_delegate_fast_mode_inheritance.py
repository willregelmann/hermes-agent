"""A subagent must not inherit the parent turn's fast-mode tier.

Fast mode is a premium output-speed tier billed at a multiple of standard rates. A subagent is
background work by construction — nobody is watching its token stream, and a backgrounded child
outlives the turn that spawned it — so inheriting the parent's tier would bill its whole run at
the premium with no one waiting on it.

``delegation.request_overrides`` is the supported way to ask for one deliberately, and it still
merges OVER the inherited set.
"""

from types import SimpleNamespace

from hermes_cli.models import FAST_MODE_REQUEST_OVERRIDES, resolve_fast_mode_overrides
from tools.delegate_tool_config import _merge_request_overrides, inherited_request_overrides


def _parent(**overrides):
    return SimpleNamespace(request_overrides=dict(overrides))


def test_fast_params_are_dropped_while_everything_else_is_inherited():
    parent = _parent(
        speed="fast",                      # Anthropic fast mode
        service_tier="priority",           # OpenAI / xAI priority processing
        prompt_cache_key="session-1",      # unrelated per-request plumbing
        extra_body={"thinking": {"type": "disabled"}},  # provider personality
    )

    inherited = inherited_request_overrides(parent)

    assert "speed" not in inherited and "service_tier" not in inherited
    # Everything that is not a fast-mode param rides along untouched.
    assert inherited == {"prompt_cache_key": "session-1", "extra_body": {"thinking": {"type": "disabled"}}}


def test_explicit_delegation_overrides_can_still_opt_a_child_in():
    """Stripping inheritance must not break the deliberate opt-in path."""
    merged = _merge_request_overrides(
        inherited_request_overrides(_parent(speed="fast", prompt_cache_key="k")),
        {"speed": "fast"},  # delegation.request_overrides
    )

    assert merged["speed"] == "fast"
    assert merged["prompt_cache_key"] == "k"


def test_an_ordinary_service_tier_is_still_inherited():
    """Only the fast VALUE is stripped. "flex" is a cheaper OpenAI tier, "default" the standard
    one — filtering by key name would quietly move every subagent off them."""
    for tier in ("default", "flex", "auto"):
        assert inherited_request_overrides(_parent(service_tier=tier)) == {"service_tier": tier}


def test_every_pair_fast_mode_can_emit_is_covered_by_the_filter():
    """The filter mapping and the emitter cannot drift as providers are added."""
    for model in ("claude-opus-5", "gpt-5.4"):
        emitted = resolve_fast_mode_overrides(model) or {}
        assert emitted.items() <= FAST_MODE_REQUEST_OVERRIDES.items(), (model, emitted)
