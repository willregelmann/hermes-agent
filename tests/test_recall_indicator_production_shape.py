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


_PASSED = {"n": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASSED["n"] += 1
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


print("\nG. NEW ARMS (wren:i27 round 20 mutation audit)")

# ---------------------------------------------------------------- G1
# The resolver-unavailable fallback. `_recall_indicator_enabled` catches an
# ImportError from gateway.display_config and reads the FLAT key with a
# default of True. Nothing drove that branch: under the suite the gateway
# import always succeeds, so flipping its default to False was invisible --
# and that default is the "unconfigured box keeps today's behaviour" promise
# on the one path where no gateway exists at all (plain CLI, the case the
# comment names).
import importlib as _importlib  # noqa: E402


class _NoDisplayConfig:
    """Make `from gateway.display_config import ...` raise, and prove it did."""

    def __enter__(self):
        self.saved = sys.modules.get("gateway.display_config", "<absent>")
        sys.modules["gateway.display_config"] = None  # import -> ImportError
        return self

    def __exit__(self, *a):
        if self.saved == "<absent>":
            sys.modules.pop("gateway.display_config", None)
        else:
            sys.modules["gateway.display_config"] = self.saved
        return False


with _NoDisplayConfig():
    _raised = False
    try:
        from gateway.display_config import resolve_display_setting  # noqa: F401
    except ImportError:
        _raised = True
    check(
        "G0 non-vacuity: the resolver import really fails in G1's fixture",
        _raised,
        "fixture does not block the import; G1 is measuring the resolver path",
    )

    _with_config({"display": {}})
    check(
        "G1 no resolver + unconfigured -> ON (flat-key default)",
        tc._recall_indicator_enabled(ProductionShapedAgent()) is True,
        "the no-gateway fallback silently disabled the indicator",
    )
    _with_config({"display": {"memory_recall_indicator": False}})
    check(
        "G1b no resolver + flat key off -> OFF (branch is load-bearing)",
        tc._recall_indicator_enabled(ProductionShapedAgent()) is False,
    )
_reset_cache()


# ---------------------------------------------------------------- G2
# B4 asserts exactly one string, "off", so the whole falsey SET was
# satisfiable by {"off"} alone. An arm asserting a carried value must range
# over the space of values it claims to cover (lessons 72/76/83).
_FALSEY = ["off", "OFF", "  Off  ", "false", "FALSE", "no", "NO", "0"]
_TRUTHY = ["on", "true", "TRUE", "yes", "1", "anything-else", ""]

_bad = []
for _s in _FALSEY:
    _with_config({"display": {"memory_recall_indicator": _s}})
    if tc._recall_indicator_enabled(ProductionShapedAgent()) is not False:
        _bad.append(_s)
check("G2 every falsey spelling disables it", not _bad, f"still enabled for {_bad}")

_bad = []
for _s in _TRUTHY:
    _with_config({"display": {"memory_recall_indicator": _s}})
    if tc._recall_indicator_enabled(ProductionShapedAgent()) is not True:
        _bad.append(_s)
check("G2b every other string leaves it on", not _bad, f"wrongly disabled for {_bad}")
_reset_cache()


# ---------------------------------------------------------------- G3
# The PR body claims: "Keying also means an edited config.yaml takes effect on
# the next turn rather than requiring a restart." No case asserted it. The
# freshness stamp lives in the cache VALUE, so dropping the `hit[:2] == stamp`
# comparison keeps one entry per path forever and pins the first answer for
# the life of the process -- which is the restart-required behaviour the key
# exists to remove. Same path, edited file.
def _edit_in_place_pair(first: dict, second: dict) -> tuple:
    import pathlib
    import hermes_cli.config as hc

    home = tempfile.mkdtemp(prefix="g3cfg-")
    cfgfile = os.path.join(home, "config.yaml")
    with open(cfgfile, "w") as fh:
        fh.write("display: {}\n")

    seq = [first, second]
    st = {"i": 0, "loads": 0}
    orig_path, orig_load = hc.get_config_path, _gr._load_gateway_config

    def fake_load():
        st["loads"] += 1
        cfg = seq[min(st["i"], 1)]
        return cfg

    hc.get_config_path = lambda: pathlib.Path(cfgfile)
    _gr._load_gateway_config = fake_load
    _reset_cache()
    try:
        a = tc._recall_indicator_enabled(ProductionShapedAgent())
        # SAME path, new bytes: the user edited config.yaml.
        st["i"] = 1
        with open(cfgfile, "w") as fh:
            fh.write("display: {}\n# edited by the user, changes size and mtime\n")
        b = tc._recall_indicator_enabled(ProductionShapedAgent())
        return [a, b], st["loads"]
    finally:
        hc.get_config_path = orig_path
        _gr._load_gateway_config = orig_load
        _reset_cache()


_res, _loads = _edit_in_place_pair(OFF, ON)
check(
    "G3 an edited config.yaml takes effect on the next turn",
    _res == [False, True],
    f"got {_res}; a stamp-blind cache gives [False, False] until restart",
)
check("G3b ...and it did so by re-reading the file", _loads == 2, f"{_loads} loads")


# ---------------------------------------------------------------- G4
# When the config path cannot be identified the code deliberately does NOT
# cache -- storing under a placeholder key is exactly the profile collision
# the key exists to prevent, and no case covered the except branch. Two turns
# with an unresolvable path must each see their own config.
def _unkeyable_pair(first: dict, second: dict) -> tuple:
    import hermes_cli.config as hc

    seq = [first, second]
    st = {"i": 0, "loads": 0}
    orig_path, orig_load = hc.get_config_path, _gr._load_gateway_config

    def boom():
        raise RuntimeError("no profile home on this box")

    def fake_load():
        cfg = seq[min(st["i"], 1)]
        st["i"] += 1
        st["loads"] += 1
        return cfg

    hc.get_config_path = boom
    _gr._load_gateway_config = fake_load
    _reset_cache()
    try:
        return [tc._recall_indicator_enabled(ProductionShapedAgent()) for _ in range(2)], st["loads"]
    finally:
        hc.get_config_path = orig_path
        _gr._load_gateway_config = orig_load
        _reset_cache()


_res, _loads = _unkeyable_pair(OFF, ON)
check(
    "G4 an unkeyable config is not cached under a placeholder key",
    _res == [False, True],
    f"got {_res}; caching under a constant key gives [False, False]",
)
check("G4b non-vacuity: both turns loaded", _loads == 2, f"{_loads} loads")


# ---------------------------------------------------------------- G6
# "LOAD UNDER THE LOCK, re-checking first" is a claim about CONCURRENT first
# callers, and E1 only ever calls sequentially -- so moving the load outside
# the lock passed. N threads arriving together must produce exactly one load.
def _concurrent_first_callers(n: int = 8) -> int:
    import pathlib
    import threading as _th
    import hermes_cli.config as hc

    home = tempfile.mkdtemp(prefix="g6cfg-")
    with open(os.path.join(home, "config.yaml"), "w") as fh:
        fh.write("display: {}\n")
    st = {"loads": 0}
    lk = _th.Lock()
    orig_path, orig_load = hc.get_config_path, _gr._load_gateway_config

    def slow_load():
        with lk:
            st["loads"] += 1
        time.sleep(0.05)  # widen the window a load-outside-the-lock would open
        return {"display": {"memory_recall_indicator": False}}

    hc.get_config_path = lambda: pathlib.Path(home, "config.yaml")
    _gr._load_gateway_config = slow_load
    _reset_cache()
    barrier = _th.Barrier(n)

    def worker():
        barrier.wait()
        tc._recall_indicator_enabled(ProductionShapedAgent())

    try:
        ts = [_th.Thread(target=worker) for _ in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        return st["loads"]
    finally:
        hc.get_config_path = orig_path
        _gr._load_gateway_config = orig_load
        _reset_cache()


import time  # noqa: E402

_loads = _concurrent_first_callers()
check(
    "G6 eight concurrent first callers cause exactly one load",
    _loads == 1,
    f"{_loads} loads; the loader ran outside the lock",
)


# ---------------------------------------------------------------- G5
# RLock, not Lock: the loader runs WITH THE LOCK HELD and late-imports
# gateway.run, so a re-entrant call must degrade to a redundant load rather
# than deadlock. Downgrading to a plain Lock hangs forever.
#
# A deadlock is not a failed assertion -- the thread never returns, holds the
# lock, and every later case blocks behind it, turning a red suite into a
# corpse a fail-counting harness reads as SURVIVED (lesson 77). So: run the
# re-entrant call in a DAEMON THREAD with a join timeout, treat "did not
# finish" as the failure, and if it did deadlock, rebind the module lock to a
# fresh RLock so the abandoned thread cannot poison the cleanup below.
# Deliberately LAST for the same reason.
#
# Not a child process: the child would need an interpreter that can import
# this tree, and under uvx/pytest sys.executable cannot -- an arm that dies of
# ModuleNotFoundError is a green light wired to nothing.
def _reentrant_load_finishes(timeout_s: float = 10.0) -> tuple:
    import pathlib
    import threading as _th
    import hermes_cli.config as hc

    home = tempfile.mkdtemp(prefix="g5cfg-")
    with open(os.path.join(home, "config.yaml"), "w") as fh:
        fh.write("display: {}\n")

    st = {"depth": 0, "loads": 0, "inner_ran": False}
    orig_path, orig_load = hc.get_config_path, _gr._load_gateway_config

    def reentrant_load():
        # Stands in for the real loader's late `import gateway.run`: any import
        # side effect that reaches back into the config path re-enters this
        # function while the lock is already held by this same thread.
        st["loads"] += 1
        if st["depth"] == 0:
            st["depth"] = 1
            tc._recall_indicator_config()  # RE-ENTRY
            st["inner_ran"] = True
        return {"display": {"memory_recall_indicator": False}}

    hc.get_config_path = lambda: pathlib.Path(home, "config.yaml")
    _gr._load_gateway_config = reentrant_load
    _reset_cache()

    out = {}

    def worker():
        out["val"] = tc._recall_indicator_enabled(ProductionShapedAgent())

    t = _th.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout_s)
    hung = t.is_alive()
    try:
        return (not hung), out.get("val"), st
    finally:
        hc.get_config_path = orig_path
        _gr._load_gateway_config = orig_load
        if hung:
            # The abandoned thread still holds the old lock. Rebind so the
            # cleanup below cannot block on it.
            import threading as _th2

            tc._RECALL_CFG_LOCK = _th2.RLock()
            tc._RECALL_CFG_CACHE.clear()
        else:
            _reset_cache()


_finished, _val, _g5st = _reentrant_load_finishes()
check(
    "G5 a re-entrant load degrades to a redundant load, never a deadlock",
    _finished,
    "the loader never returned: a non-reentrant lock deadlocks the config read",
)
check(
    "G5b non-vacuity: the fixture really did re-enter the loader",
    _g5st["inner_ran"] and _g5st["loads"] >= 2,
    f"no re-entry happened, so G5 proved nothing: {_g5st}",
)
check(
    "G5c ...and the re-entrant turn still returns the configured answer",
    _val is False,
    f"got {_val}",
)

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


CASE_FLOOR = 28  # MEASURED after the round-20 run, not guessed (lessons 79e / 83f).


def test_case_floor() -> None:
    """A script suite can die halfway and still print no FAIL line.

    The count is the only thing that distinguishes "every case ran and passed"
    from "the module raised at case 12 and the harness saw zero failures"
    (lesson 77). Set from the measured count AFTER the mutation run.
    """
    ran = len(FAILS) + _PASSED["n"]
    assert ran >= CASE_FLOOR, f"only {ran} cases ran, floor is {CASE_FLOOR}"


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
    ran = len(FAILS) + _PASSED["n"]
    if ran < CASE_FLOOR:
        print(f"  ABORTED: only {ran} cases ran, floor is {CASE_FLOOR}")
        sys.exit(1)
    print(f"  ALL PASS ({ran} cases)")
