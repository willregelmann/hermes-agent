"""Tests for the configurable default interface (cli vs tui).

`hermes` launches the classic prompt_toolkit REPL by default, but users can
flip ``display.interface: tui`` in config.yaml to make the modern Ink TUI the
default for bare ``hermes`` / ``hermes chat``. Explicit flags always win:

    --cli                forces the classic REPL (highest precedence)
    --tui                forces the TUI
    (no TTY)             forces the classic REPL — ambient prefs don't apply
    HERMES_TUI=1         the env default
    display.interface    the configured default
    (unset)              classic REPL

The no-TTY gate exists because ambient TUI preferences must never hijack
non-interactive invocations: kanban workers / cron / pipelines run
``hermes … chat -q`` on a pipe, and the TUI's no-TTY bail-out exits 0
without doing the work (a kanban worker then dies with "protocol
violation" on every attempt).

These tests pin that precedence at every layer that makes the decision:

  * ``_resolve_use_tui(args)``  — the canonical args-aware resolver used by
    ``cmd_chat`` and the Termux fast-TUI path.
  * ``_wants_tui_early(argv)``  — the dependency-free early resolver used by
    mouse-residue suppression and the Termux fast paths, before argparse and
    ``hermes_cli.config`` are importable.
  * the argument parser   — both ``--cli`` and ``--tui`` parse at the top
    level and under the ``chat`` subcommand and are relaunch-inherited.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from hermes_cli import main as m


@pytest.fixture(autouse=True)
def _reset_early_cache(monkeypatch):
    # The early resolver memoizes the config read; clear it so each test sees
    # a fresh value, and make sure no stray HERMES_TUI leaks in.
    monkeypatch.setattr(m, "_EARLY_INTERFACE_CACHE", {})
    monkeypatch.delenv("HERMES_TUI", raising=False)
    yield
    monkeypatch.setattr(m, "_EARLY_INTERFACE_CACHE", {})


def _args(**kw):
    kw.setdefault("cli", False)
    kw.setdefault("tui", False)
    return SimpleNamespace(**kw)


def _fake_tty(monkeypatch, interactive: bool):
    """Pin stdin/stdout TTY-ness — pytest's capture is never a real TTY."""
    import sys as _sys

    monkeypatch.setattr(_sys.stdin, "isatty", lambda: interactive, raising=False)
    monkeypatch.setattr(_sys.stdout, "isatty", lambda: interactive, raising=False)


def _patch_config(monkeypatch, interface):
    import hermes_cli.config as cfg

    monkeypatch.setattr(
        cfg, "load_config", lambda: {"display": {"interface": interface}}
    )


# ---------------------------------------------------------------------------
# _resolve_use_tui — args-aware resolver
# ---------------------------------------------------------------------------
class TestResolveUseTui:
    def test_cli_flag_beats_config_tui(self, monkeypatch):
        _patch_config(monkeypatch, "tui")
        assert m._resolve_use_tui(_args(cli=True)) is False


    def test_load_config_failure_falls_back_to_cli(self, monkeypatch):
        import hermes_cli.config as cfg

        def boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(cfg, "load_config", boom)
        _fake_tty(monkeypatch, True)
        assert m._resolve_use_tui(_args()) is False

    # ── the no-TTY gate: ambient prefs never hijack non-interactive runs ────


# ---------------------------------------------------------------------------
# _wants_tui_early — dependency-free early resolver
# ---------------------------------------------------------------------------
class TestWantsTuiEarly:
    @pytest.fixture
    def home_with_interface(self, tmp_path, monkeypatch):
        def _make(interface):
            (tmp_path / "config.yaml").write_text(
                f"display:\n  interface: {interface}\n"
            )
            monkeypatch.setenv("HERMES_HOME", str(tmp_path))
            monkeypatch.setattr(m, "_EARLY_INTERFACE_CACHE", {})

        return _make


    def test_config_cli_bare_argv(self, home_with_interface):
        home_with_interface("cli")
        assert m._wants_tui_early([]) is False

    def test_missing_config_defaults_to_cli(self, tmp_path, monkeypatch):
        # HERMES_HOME points at an empty dir — no config.yaml.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(m, "_EARLY_INTERFACE_CACHE", {})
        assert m._wants_tui_early([]) is False

    def test_unreadable_config_defaults_to_cli(self, tmp_path, monkeypatch):
        # Garbage YAML must not crash the hot path; falls back to cli.
        (tmp_path / "config.yaml").write_text("this: : : not valid yaml\n")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(m, "_EARLY_INTERFACE_CACHE", {})
        assert m._wants_tui_early([]) is False


# ---------------------------------------------------------------------------
# argument parser — flags exist at both levels and are relaunch-inherited
# ---------------------------------------------------------------------------
class TestParserFlags:
    def _parser(self):
        from hermes_cli._parser import build_top_level_parser

        parser, _subparsers, _chat = build_top_level_parser()
        return parser

    def test_top_level_cli_flag(self):
        args = self._parser().parse_args(["--cli"])
        assert args.cli is True and args.tui is False


    def test_chat_subcommand_tui_flag(self):
        args = self._parser().parse_args(["chat", "--tui"])
        assert args.tui is True

    def test_cli_and_tui_are_relaunch_inherited(self):
        from hermes_cli.relaunch import _INHERITED_FLAGS_TABLE

        inherited = {flag for flag, _takes_value in _INHERITED_FLAGS_TABLE}
        assert "--cli" in inherited
        assert "--tui" in inherited


# ---------------------------------------------------------------------------
# config default — shipped default preserves classic behavior
# ---------------------------------------------------------------------------
def test_default_config_interface_is_cli():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["display"]["interface"] == "cli"


# ---------------------------------------------------------------------------
# _EARLY_INTERFACE_CACHE — the cache must be scoped to the home it read
# ---------------------------------------------------------------------------
class TestEarlyInterfaceCacheIsProfileScoped:
    """The early interface read runs at import time, which is BEFORE
    ``_apply_profile_override()`` points ``HERMES_HOME`` at the profile (for
    ``--profile X`` and for a sticky ``active_profile``). A cache keyed on
    nothing therefore serves the ROOT profile's ``display.interface`` to every
    later caller running under the profile's home.

    These assert the relationship (answer follows the home in effect at the
    time of the call), not a frozen value.
    """

    @staticmethod
    def _home(tmp_path, name, interface):
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(f"display:\n  interface: {interface}\n")
        return d

    def test_home_change_between_calls_is_honored(self, tmp_path, monkeypatch):
        root = self._home(tmp_path, "root", "cli")
        prof = self._home(tmp_path, "root/profiles/p1", "tui")
        monkeypatch.setattr(m, "_EARLY_INTERFACE_CACHE", {})

        monkeypatch.setenv("HERMES_HOME", str(root))
        assert m._config_default_interface_early() == "cli"
        # _apply_profile_override() does exactly this, after the first read.
        monkeypatch.setenv("HERMES_HOME", str(prof))
        assert m._config_default_interface_early() == "tui"

    def test_home_change_between_calls_is_honored_other_direction(
        self, tmp_path, monkeypatch
    ):
        # Both directions: a tui root must not force tui onto a cli profile.
        root = self._home(tmp_path, "root", "tui")
        prof = self._home(tmp_path, "root/profiles/p1", "cli")
        monkeypatch.setattr(m, "_EARLY_INTERFACE_CACHE", {})

        monkeypatch.setenv("HERMES_HOME", str(root))
        assert m._config_default_interface_early() == "tui"
        monkeypatch.setenv("HERMES_HOME", str(prof))
        assert m._config_default_interface_early() == "cli"

    def test_repeat_call_under_same_home_does_not_reparse(
        self, tmp_path, monkeypatch
    ):
        # The cache must still BE a cache: a second call under an unchanged
        # home must not touch the file again (that is the whole point of it).
        root = self._home(tmp_path, "root", "tui")
        monkeypatch.setattr(m, "_EARLY_INTERFACE_CACHE", {})
        monkeypatch.setenv("HERMES_HOME", str(root))
        assert m._config_default_interface_early() == "tui"

        calls = []
        real_exists = m.os.path.exists

        def counting_exists(path):
            calls.append(path)
            return real_exists(path)

        monkeypatch.setattr(m.os.path, "exists", counting_exists)
        assert m._config_default_interface_early() == "tui"
        assert calls == []

    def test_key_and_value_share_one_resolver(self, tmp_path, monkeypatch):
        # The cache key must be the path actually read, so a caller can never
        # store one home's answer under another home's key.
        root = self._home(tmp_path, "root", "tui")
        monkeypatch.setattr(m, "_EARLY_INTERFACE_CACHE", {})
        monkeypatch.setenv("HERMES_HOME", str(root))
        assert m._config_default_interface_early() == "tui"
        assert list(m._EARLY_INTERFACE_CACHE) == [m._early_interface_config_path()]
        assert m._early_interface_config_path() == str(root / "config.yaml")


# ---------------------------------------------------------------------------
# Precedence, the TRUE direction — restored after a suite-wide prune
# ---------------------------------------------------------------------------
# Commit 6b81590c55 ("prune low-value tests, wave 1") removed every case that
# asserted a TUI decision resolving to True, plus both no-TTY gate cases.  What
# survived only ever asserts False, so a resolver that returned False
# unconditionally — or one with the --cli branch, the --tui branch, the TTY
# gate or the error fallback deleted — passed the whole file.  Measured: eight
# separate one-branch mutants of `_wants_tui_early` / `_resolve_use_tui`
# survived all 13 remaining cases, including deletion of the no-TTY gate that
# commit b06e2f846c exists to add (its absence is the kanban-worker
# "protocol violation" crash).
#
# These assert the relationship each branch encodes, not a frozen value.


class TestEarlyPrecedenceTrueDirection:
    """`_wants_tui_early` — the branches whose only observable is True."""

    @staticmethod
    def _home(tmp_path, monkeypatch, interface):
        (tmp_path / "config.yaml").write_text(
            f"display:\n  interface: {interface}\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(m, "_EARLY_INTERFACE_CACHE", {})

    def test_config_tui_boots_tui_on_a_tty(self, tmp_path, monkeypatch):
        # The whole point of display.interface: tui. Nothing asserted it.
        self._home(tmp_path, monkeypatch, "tui")
        _fake_tty(monkeypatch, True)
        assert m._wants_tui_early([]) is True

    def test_no_tty_blocks_config_tui(self, tmp_path, monkeypatch):
        # The load-bearing gate: ambient config must not hijack a pipe.
        self._home(tmp_path, monkeypatch, "tui")
        _fake_tty(monkeypatch, False)
        assert m._wants_tui_early([]) is False

    def test_cli_flag_beats_config_tui_on_a_tty(self, tmp_path, monkeypatch):
        # Without a TTY this passes even with the --cli branch deleted.
        self._home(tmp_path, monkeypatch, "tui")
        _fake_tty(monkeypatch, True)
        assert m._wants_tui_early(["--cli"]) is False

    def test_explicit_tui_flag_survives_no_tty(self, tmp_path, monkeypatch):
        # --tui is checked BEFORE the TTY gate: the user asked explicitly and
        # gets the TUI's informative bail-out rather than silence.
        self._home(tmp_path, monkeypatch, "cli")
        _fake_tty(monkeypatch, False)
        assert m._wants_tui_early(["--tui"]) is True

    def test_env_tui_survives_no_tty(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch, "cli")
        monkeypatch.setenv("HERMES_TUI", "1")
        _fake_tty(monkeypatch, False)
        assert m._wants_tui_early([]) is True

    def test_unreadable_config_falls_back_to_cli_not_tui(
        self, tmp_path, monkeypatch
    ):
        # Reached directly: _wants_tui_early's TTY gate returns before the
        # config read under pytest capture, so the existing no-TTY case can
        # never observe the except-branch's value.
        (tmp_path / "config.yaml").write_text("this: : : not valid yaml\n")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(m, "_EARLY_INTERFACE_CACHE", {})
        assert m._config_default_interface_early() == "cli"


class TestResolveUseTuiPrecedenceTrueDirection:
    """`_resolve_use_tui` — same branches, the args-aware resolver."""

    def test_config_tui_boots_tui_on_a_tty(self, monkeypatch):
        _patch_config(monkeypatch, "tui")
        _fake_tty(monkeypatch, True)
        assert m._resolve_use_tui(_args()) is True

    def test_cli_flag_beats_config_tui_on_a_tty(self, monkeypatch):
        _patch_config(monkeypatch, "tui")
        _fake_tty(monkeypatch, True)
        assert m._resolve_use_tui(_args(cli=True)) is False

    def test_tui_flag_survives_no_tty(self, monkeypatch):
        _patch_config(monkeypatch, "cli")
        _fake_tty(monkeypatch, False)
        assert m._resolve_use_tui(_args(tui=True)) is True

    def test_no_tty_blocks_env_tui(self, monkeypatch):
        _patch_config(monkeypatch, "cli")
        monkeypatch.setenv("HERMES_TUI", "1")
        _fake_tty(monkeypatch, False)
        assert m._resolve_use_tui(_args()) is False

    def test_isatty_raising_falls_back_to_classic(self, monkeypatch):
        # The TTY probe's own except-branch: a stdio object whose isatty()
        # explodes must not be treated as interactive.
        import sys as _sys

        def boom():
            raise ValueError("I/O operation on closed file")

        monkeypatch.setattr(_sys.stdin, "isatty", boom, raising=False)
        monkeypatch.setenv("HERMES_TUI", "1")
        assert m._resolve_use_tui(_args()) is False

    def test_interface_value_is_case_insensitive(self, monkeypatch):
        _patch_config(monkeypatch, "TUI")
        _fake_tty(monkeypatch, True)
        assert m._resolve_use_tui(_args()) is True
