"""Home-scoped $HERMES_HOME provider discovery (wren:i24, final member).

Proves the two bugs fixed together:

1. A bare-global _REGISTRY/_ALIASES meant the first profile's
   $HERMES_HOME/plugins/model-providers/<name>/ registrations pinned every
   OTHER profile's list_providers()/get_provider_profile() output in the
   same process, including a bundled-name override — until restart.
2. The sys.modules import guard for user plugins was keyed by plugin
   dirname alone, so two profiles with a SAME-NAMED but DIFFERENT plugin
   directory collided: the second profile's import was silently skipped as
   "already imported" and its registration never happened at all.

Both are exercised against real directories and real imports through
providers._discover_home_providers -- no mocking of the import machinery,
because the import machinery IS the subject.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

import providers
from hermes_constants import hermes_home_key, set_hermes_home_override


def _clear_all_provider_state():
    providers._REGISTRY.clear()
    providers._ALIASES.clear()
    providers._SCOPED_REGISTRY.clear()
    providers._SCOPED_ALIASES.clear()
    providers._PROVIDER_LIST_CACHE.clear()
    providers._discovered = False
    providers._discovered_homes.clear()
    for mod in list(sys.modules.keys()):
        if mod.startswith("plugins.model_providers") or mod.startswith(
            "_hermes_user_provider"
        ):
            del sys.modules[mod]


@pytest.fixture(autouse=True)
def _isolate_and_restore():
    _clear_all_provider_state()
    yield
    _clear_all_provider_state()
    providers._discover_providers()


def _make_home_with_plugin(tmp_path: Path, home_name: str, plugin_name: str, provider_name: str, aliases=()) -> Path:
    home = tmp_path / home_name
    plugin_dir = home / "plugins" / "model-providers" / plugin_name
    plugin_dir.mkdir(parents=True)
    aliases_repr = repr(tuple(aliases))
    (plugin_dir / "__init__.py").write_text(
        textwrap.dedent(
            f"""
            from providers import register_provider, ProviderProfile

            register_provider(ProviderProfile(name={provider_name!r}, aliases={aliases_repr}))
            """
        )
    )
    return home


def test_two_homes_same_plugin_name_do_not_pin_each_other(tmp_path):
    """Bug 1: a bare global let home A's user plugin registration answer
    for home B too, once A was discovered first in the process."""
    home_a = _make_home_with_plugin(tmp_path, "home_a", "acme", "acme-a")
    home_b = _make_home_with_plugin(tmp_path, "home_b", "acme", "acme-b")

    providers._discover_providers()  # global sources only, once

    token_a = set_hermes_home_override(str(home_a))
    try:
        assert providers.get_provider_profile("acme-a") is not None
        assert providers.get_provider_profile("acme-b") is None
    finally:
        from hermes_constants import reset_hermes_home_override
        reset_hermes_home_override(token_a)

    token_b = set_hermes_home_override(str(home_b))
    try:
        assert providers.get_provider_profile("acme-b") is not None
        # This is the exact defect: pre-fix, home B's list_providers()/
        # get_provider_profile() would still see home A's "acme-a" because
        # both wrote into the same bare _REGISTRY and A discovered first.
        assert providers.get_provider_profile("acme-a") is None
    finally:
        from hermes_constants import reset_hermes_home_override
        reset_hermes_home_override(token_b)


def test_two_homes_same_dirname_both_actually_import(tmp_path):
    """Bug 2: the sys.modules guard keyed by dirname alone skipped the
    SECOND home's same-named-but-different plugin dir as "already imported",
    so its register_provider() call never ran."""
    home_a = _make_home_with_plugin(tmp_path, "home_a2", "widget", "widget-a")
    home_b = _make_home_with_plugin(tmp_path, "home_b2", "widget", "widget-b")

    from hermes_constants import reset_hermes_home_override

    token_a = set_hermes_home_override(str(home_a))
    try:
        providers._ensure_discovered()
        assert providers.get_provider_profile("widget-a") is not None
    finally:
        reset_hermes_home_override(token_a)

    token_b = set_hermes_home_override(str(home_b))
    try:
        providers._ensure_discovered()
        # Pre-fix: module_name was f"_hermes_user_provider_widget" for BOTH
        # homes, so this second import hit `if module_name in sys.modules:
        # return` and widget-b was NEVER registered.
        assert providers.get_provider_profile("widget-b") is not None
    finally:
        reset_hermes_home_override(token_b)


def test_home_scoped_plugin_overrides_bundled_only_for_its_own_home(tmp_path):
    """Override semantics preserved (last-writer-wins), but scoped: a user
    plugin overriding a bundled name in home A must not leak the override
    into home B, which should still see the bundled profile."""
    bundled = providers.get_provider_profile("openrouter")
    assert bundled is not None, "test assumes 'openrouter' ships bundled"

    home_a = _make_home_with_plugin(tmp_path, "home_override", "openrouter", "openrouter")
    providers._discover_providers()

    from hermes_constants import reset_hermes_home_override

    token_a = set_hermes_home_override(str(home_a))
    try:
        overridden = providers.get_provider_profile("openrouter")
        assert overridden is not bundled
    finally:
        reset_hermes_home_override(token_a)

    unrelated_home = home_a.parent / "unrelated_home"
    unrelated_home.mkdir()
    token_b = set_hermes_home_override(str(unrelated_home))
    try:
        # Unrelated home never discovered this plugin dir (it doesn't exist
        # under it) -> must fall back to the bundled profile, not home A's
        # override.
        assert providers.get_provider_profile("openrouter") is bundled
    finally:
        reset_hermes_home_override(token_b)


def test_list_providers_merges_home_scope_over_global(tmp_path):
    home = _make_home_with_plugin(tmp_path, "home_list", "brandnew", "brandnew")
    providers._discover_providers()

    from hermes_constants import reset_hermes_home_override

    baseline_names = {p.name for p in providers.list_providers()}
    assert "brandnew" not in baseline_names

    token = set_hermes_home_override(str(home))
    try:
        names = {p.name for p in providers.list_providers()}
        assert "brandnew" in names
    finally:
        reset_hermes_home_override(token)

    # Back to no override: brandnew must not have leaked into the global list.
    assert "brandnew" not in {p.name for p in providers.list_providers()}
