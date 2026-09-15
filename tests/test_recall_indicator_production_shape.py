"""The gate must work on an agent built the way PRODUCTION builds one.

WHY THIS FILE EXISTS, separately from tests/test_memory_recall_indicator.py:

``_recall_indicator_enabled`` shipped inert. It read
``getattr(agent, "user_config", None) or {}``, and no agent object in the tree
is ever given a ``user_config`` attribute — a tree-wide grep finds exactly one
assignment, in the other suite's fixture. So ``cfg`` was ``{}`` on every
production turn and no config value could disable the indicator.

Two independent suites passed anyway (19 cases here, 14 on Wren's box), because
BOTH fixtures constructed an agent with the attribute already set. Reproducing a
claim with the same fixture assumption reproduces the assumption, not the
behaviour. The review swap did not catch it; only reading production did.

THE RULE THIS ENCODES: at least one case must build its subject the way
production does. Concretely here — an agent object that carries ONLY what
``agent_init.py`` actually sets (``platform``), with no ``user_config`` — and
the config must still take effect.

These cases FAIL against the pre-fix function. That is the point: if someone
reverts to the getattr-only form, case B goes red.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.turn_context as tc  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  :: {detail}")


class ProductionShapedAgent:
    """Exactly what agent_init.py leaves on the object, and nothing more.

    agent_init.py:679 sets ``agent.platform``. It does NOT set
    ``user_config``. Deliberately no such attribute here — that absence IS
    the subject under test.
    """

    def __init__(self, platform: str = "cli") -> None:
        self.platform = platform


def _reset_cache() -> None:
    tc._recall_indicator_cache_clear()


import gateway.run as _gr  # noqa: E402

_REAL_LOADER = _gr._load_gateway_config


def _with_config(cfg: dict):
    """Point the REAL loader at a known config.

    Deliberately not poking a module global: the cache is keyed off the config
    path now, and a test that writes past the key would stop exercising the
    thing under test. This drives the same code path production drives.
    """
    _reset_cache()
    _gr._load_gateway_config = lambda: cfg


print("A. the defect itself — the attribute production never sets")

a = ProductionShapedAgent()
check(
    "A1 production agent genuinely lacks user_config",
    not hasattr(a, "user_config"),
    "fixture is not production-shaped; the test proves nothing",
)

_reset_cache()
check(
    "A2 default is on for an unconfigured box",
    tc._recall_indicator_enabled(a) is True,
)


print("\nB. THE REGRESSION CASE — config must reach a production-shaped agent")

_with_config({"display": {"memory_recall_indicator": False}})
check(
    "B1 global off disables it WITHOUT user_config on the agent",
    tc._recall_indicator_enabled(ProductionShapedAgent()) is False,
    "gate is inert: config had no effect on a production-shaped agent",
)

_with_config(
    {
        "display": {
            "memory_recall_indicator": True,
            "platforms": {"google_chat": {"memory_recall_indicator": False}},
        }
    }
)
check(
    "B2 per-platform off beats global on",
    tc._recall_indicator_enabled(ProductionShapedAgent("google_chat")) is False,
)
check(
    "B3 ...and other platforms keep the global value",
    tc._recall_indicator_enabled(ProductionShapedAgent("cli")) is True,
)

_with_config({"display": {"memory_recall_indicator": "off"}})
check(
    "B4 string 'off' honoured on a production-shaped agent",
    tc._recall_indicator_enabled(ProductionShapedAgent()) is False,
)


print("\nC. injected config still wins (the old path is not broken)")


class InjectedAgent(ProductionShapedAgent):
    def __init__(self, cfg, platform="cli"):
        super().__init__(platform)
        self.user_config = cfg


_with_config({"display": {"memory_recall_indicator": True}})
check(
    "C1 explicit user_config overrides the loaded config",
    tc._recall_indicator_enabled(
        InjectedAgent({"display": {"memory_recall_indicator": False}})
    )
    is False,
)

_with_config({"display": {"memory_recall_indicator": False}})
check(
    "C2 empty injected config falls through to the real config",
    tc._recall_indicator_enabled(InjectedAgent({})) is False,
    "an empty dict must not shadow the real config — that was the bug",
)


print("\nD. failure modes fall back to ON (never silently disable a signal)")

_reset_cache()


class Exploding:
    @property
    def platform(self):
        raise RuntimeError("boom")


check(
    "D1 raising agent falls back to True",
    tc._recall_indicator_enabled(Exploding()) is True,
)

_with_config({"display": None})
check(
    "D2 malformed display section falls back to True",
    tc._recall_indicator_enabled(ProductionShapedAgent()) is True,
)


print("\nE. the cache is a cache, and it is KEYED (Wren's profile finding)")

_reset_cache()
calls = {"n": 0}
_orig = _gr._load_gateway_config

# E1 needs a RESOLVABLE config path, because the cache deliberately declines
# to cache when it cannot identify the file (an unkeyable load must not be
# stored under a key that could collide with another profile's). Standalone
# this file inherits the real ~/.hermes; under pytest the path may not
# resolve, and the un-pinned version of this case then measured 5 loads and
# went red only under pytest. Pin the path so the case tests caching rather
# than testing the ambient environment.
import hermes_cli.config as _hc  # noqa: E402
import pathlib as _pathlib  # noqa: E402

_e1_home = tempfile.mkdtemp(prefix="e1cfg-")
open(os.path.join(_e1_home, "config.yaml"), "w").write("display: {}\n")
_orig_path = _hc.get_config_path
_hc.get_config_path = lambda: _pathlib.Path(_e1_home, "config.yaml")
_gr._load_gateway_config = lambda: (
    calls.__setitem__("n", calls["n"] + 1)
    or {"display": {"memory_recall_indicator": False}}
)
try:
    for _ in range(5):
        tc._recall_indicator_enabled(ProductionShapedAgent())
    check("E1 config loaded once across 5 turns", calls["n"] == 1, f"loaded {calls['n']}x")
finally:
    _gr._load_gateway_config = _orig
    _hc.get_config_path = _orig_path
    _reset_cache()


def _profile_pair(first: dict, second: dict) -> list:
    """Two turns whose config path differs — i.e. two profiles.

    The bug: an UNKEYED module global let the first caller pin the answer for
    every later caller regardless of which config was live. Simulated here by
    a loader that returns a different config each call while the cache key
    changes with it.
    """
    _reset_cache()
    seq = [first, second]
    st = {"i": 0}
    homes = [
        tempfile.mkdtemp(prefix="profA-"),
        tempfile.mkdtemp(prefix="profB-"),
    ]
    for h, c in zip(homes, seq):
        with open(os.path.join(h, "config.yaml"), "w") as fh:
            fh.write("display: {}\n" if not c else "display: {}\n")

    import hermes_cli.config as hc

    orig_path = hc.get_config_path
    orig_load = _gr._load_gateway_config
    import pathlib

    def fake_path():
        return pathlib.Path(homes[min(st["i"], 1)], "config.yaml")

    def fake_load():
        cfg = seq[min(st["i"], 1)]
        st["i"] += 1
        return cfg

    # turn_context late-imports get_config_path from hermes_cli.config
    hc.get_config_path = fake_path
    _gr._load_gateway_config = fake_load
    try:
        out = []
        for _ in range(2):
            out.append(tc._recall_indicator_enabled(ProductionShapedAgent()))
        return out
    finally:
        hc.get_config_path = orig_path
        _gr._load_gateway_config = orig_load
        _reset_cache()


OFF = {"display": {"memory_recall_indicator": False}}
ON = {"display": {"memory_recall_indicator": True}}

res = _profile_pair(OFF, ON)
check(
    "E2 profile B is NOT pinned by profile A (off then on)",
    res == [False, True],
    f"got {res}; unkeyed cache would give [False, False]",
)

res = _profile_pair(ON, OFF)
check(
    "E3 profile B is NOT pinned by profile A (on then off)",
    res == [True, False],
    f"got {res}; unkeyed cache would give [True, True]",
)


print("\nF. MUTANT — revert to the getattr-only form; B1 must die")

_src_fn = tc._recall_indicator_enabled


def mutant(agent):
    """The shipped-inert original."""
    try:
        cfg = getattr(agent, "user_config", None) or {}
        platform = getattr(agent, "platform", "") or ""
        try:
            from gateway.display_config import resolve_display_setting

            val = resolve_display_setting(cfg, platform, "memory_recall_indicator", True)
        except Exception:
            val = (cfg.get("display") or {}).get("memory_recall_indicator", True)
        if isinstance(val, str):
            return val.strip().lower() not in {"off", "false", "no", "0"}
        return bool(val)
    except Exception:
        return True


_with_config({"display": {"memory_recall_indicator": False}})
mutant_result = mutant(ProductionShapedAgent())
check(
    "F1 MUTANT ignores config on a production agent (killed by B1)",
    mutant_result is True,
    f"mutant returned {mutant_result}; it should be inert",
)
check(
    "F2 non-vacuity: the real function DOES honour that same config",
    tc._recall_indicator_enabled(ProductionShapedAgent()) is False,
)
_reset_cache()

# ── CLEAN UP THE MONKEYPATCH ──────────────────────────────────────────────
# `_with_config` replaces gateway.run._load_gateway_config and never restores
# it. Standalone that is harmless (the process exits); under pytest this file
# shares an interpreter with tests/test_memory_recall_indicator.py, and the
# stale loader made two of ITS cases fail — green alone, red together, with
# the blame landing on the innocent file. Restore explicitly.
_gr._load_gateway_config = _REAL_LOADER
_reset_cache()


def test_no_cross_file_pollution() -> None:
    """The loader this file patched is the real one again."""
    assert _gr._load_gateway_config is _REAL_LOADER


def test_recall_indicator_production_shape() -> None:
    """pytest entry point.

    The cases above run at import (script style, so the file is runnable
    directly). Without this function pytest collects the module, executes the
    body, and reports "no tests ran" — a green run that gated nothing. Same
    silence-as-success shape as the defect this file exists for.
    """
    assert not FAILS, f"{len(FAILS)} failed: {FAILS}"


if __name__ == "__main__":
    print()
    if FAILS:
        print(f"  {len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("  ALL PASS")
