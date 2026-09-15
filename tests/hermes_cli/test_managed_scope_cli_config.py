"""Managed scope must reach cli.py's independent config loader (CLI_CONFIG).

cli.py's load_cli_config() builds config separately from
hermes_cli.config._load_config_impl, so the managed-scope merge has to be
applied in BOTH places or the interactive CLI/TUI surface (skin, display prefs)
silently ignores administrator-pinned values while `hermes config`/`doctor`
honor them. This locks the cli.py path.
"""
import importlib

import pytest


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()
    return home, managed


def _load_cli_config(home):
    """Call cli.py's standalone loader fresh.

    ``load_cli_config()`` resolves ``get_hermes_home()`` on every call (#11), so
    the ``homes`` fixture's ``HERMES_HOME`` is enough to move it -- no module
    constant needs patching. ``home`` is accepted so call sites read the same
    and is asserted against the live resolver, which is what the loader will
    actually use.
    """
    import cli
    from hermes_constants import get_hermes_home

    assert str(get_hermes_home()) == str(home), (
        "the live home is not the test home; this case would measure the wrong "
        "config file"
    )
    return cli.load_cli_config()


def test_cli_config_honors_managed_skin(homes):
    """A managed display.skin must reach CLI_CONFIG (the TUI's source)."""
    home, managed = homes
    (home / "config.yaml").write_text("display:\n  skin: user_skin\n", encoding="utf-8")
    (managed / "config.yaml").write_text("display:\n  skin: charizard\n", encoding="utf-8")
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    cfg = _load_cli_config(home)
    assert (cfg.get("display") or {}).get("skin") == "charizard"


def test_cli_config_managed_leaf_preserves_user_siblings(homes):
    """Managed display.skin must not wipe a user's other display.* prefs."""
    home, managed = homes
    (home / "config.yaml").write_text(
        "display:\n  skin: user_skin\n  show_reasoning: true\n", encoding="utf-8"
    )
    (managed / "config.yaml").write_text("display:\n  skin: charizard\n", encoding="utf-8")
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    cfg = _load_cli_config(home)
    display = cfg.get("display") or {}
    assert display.get("skin") == "charizard"  # managed wins
    assert display.get("show_reasoning") is True  # user sibling preserved


