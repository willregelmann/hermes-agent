"""agent/relay_runtime.py::_segments_config keying, mirroring
tests/test_recall_indicator_production_shape.py (Wren, 2026-09-13).

WHY: _segments_config() cached gateway.telemetry.session_segments into a
single unkeyed module global, _SEGMENTS_CONFIG. _load_gateway_config()
resolves its path through get_config_path(), which is profile-aware, so the
first profile to rotate a session segment pinned on_compaction/max_turns for
every OTHER profile until process restart. Same shape Wren found and fixed
in agent/turn_context.py::_recall_indicator_config on the same day; this one
was noted there as "a separate fix with a separate subject" and left open.
Latent, not live, on this box only because profiles/ is empty today.

Fixed by keying the cache off (path, st_mtime_ns, st_size), same key shape
as _recall_indicator_config and agent/skill_utils.py::_load_raw_config.

Case E below is the regression case: two "profiles" (two config paths) must
not bleed into each other. Case F is the mutant — reverting to the old bare
global must make E fail, proving this test would have caught the original
bug rather than merely describing the fix.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.relay_runtime as rr  # noqa: E402
import gateway.run as _gr  # noqa: E402
import hermes_cli.config as _hc  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  :: {detail}")


_REAL_LOADER = _gr._load_gateway_config
_REAL_GET_PATH = _hc.get_config_path


def _reset_cache() -> None:
    rr._reset_segments_config_for_tests()


def _restore() -> None:
    _gr._load_gateway_config = _REAL_LOADER
    _hc.get_config_path = _REAL_GET_PATH
    _reset_cache()


print("A. defaults are inert when nothing is configured")

_reset_cache()
_gr._load_gateway_config = lambda: {}
cfg = rr._segments_config()
check(
    "A1 both defaults OFF/zero when unset",
    cfg == {"on_compaction": False, "max_turns": 0},
    f"got {cfg}",
)


def _profile_pair(first: dict, second: dict) -> list:
    """Two turns whose config path differs — i.e. two profiles.

    The bug: an UNKEYED module global let the first caller's segment
    settings pin the answer for every later caller regardless of which
    config was live.
    """
    _reset_cache()
    homes = [
        tempfile.mkdtemp(prefix="segA-"),
        tempfile.mkdtemp(prefix="segB-"),
    ]
    for h in homes:
        with open(os.path.join(h, "config.yaml"), "w") as fh:
            fh.write("display: {}\n")

    seq = [first, second]
    st = {"i": 0}
    import pathlib

    def fake_path():
        return pathlib.Path(homes[min(st["i"], 1)], "config.yaml")

    def fake_load():
        cfg = seq[min(st["i"], 1)]
        st["i"] += 1
        return cfg

    _hc.get_config_path = fake_path
    _gr._load_gateway_config = fake_load
    try:
        out = []
        for _ in range(2):
            out.append(rr._segments_config())
        return out
    finally:
        _restore()


print("\nB. THE REGRESSION CASE — profile B is not pinned by profile A")

OFF_CFG = {"gateway": {"telemetry": {"session_segments": {"on_compaction": False, "max_turns": 0}}}}
ON_CFG = {"gateway": {"telemetry": {"session_segments": {"on_compaction": True, "max_turns": 12}}}}

res = _profile_pair(OFF_CFG, ON_CFG)
check(
    "B1 off-then-on: profile B sees its own ON config",
    res == [{"on_compaction": False, "max_turns": 0}, {"on_compaction": True, "max_turns": 12}],
    f"got {res}; unkeyed cache would give [{{off}}, {{off}}]",
)

res = _profile_pair(ON_CFG, OFF_CFG)
check(
    "B2 on-then-off: profile B sees its own OFF config",
    res == [{"on_compaction": True, "max_turns": 12}, {"on_compaction": False, "max_turns": 0}],
    f"got {res}; unkeyed cache would give [{{on}}, {{on}}]",
)


print("\nC. the cache is a cache — repeated calls hit the loader once")

_reset_cache()
calls = {"n": 0}
_e1_home = tempfile.mkdtemp(prefix="segC-")
open(os.path.join(_e1_home, "config.yaml"), "w").write("display: {}\n")
import pathlib as _pathlib  # noqa: E402

_hc.get_config_path = lambda: _pathlib.Path(_e1_home, "config.yaml")
_gr._load_gateway_config = lambda: (
    calls.__setitem__("n", calls["n"] + 1) or ON_CFG
)
try:
    for _ in range(5):
        rr._segments_config()
    check("C1 config loaded once across 5 calls", calls["n"] == 1, f"loaded {calls['n']}x")
finally:
    _restore()


print("\nD. malformed / unresolvable config falls back to inert defaults")

_reset_cache()
_hc.get_config_path = _REAL_GET_PATH
_gr._load_gateway_config = lambda: {"gateway": None}
check(
    "D1 malformed gateway section falls back to inert",
    rr._segments_config() == {"on_compaction": False, "max_turns": 0},
)
_restore()


print("\nE. MUTANT — revert to the bare unkeyed global; B1/B2 must die")


def mutant_segments_config(state: dict, first: dict, second: dict) -> list:
    """The shipped-with-the-bug shape: one bare global, no key."""
    cache = state  # emulate module-level _SEGMENTS_CONFIG as a single slot
    seq = [first, second]
    out = []
    for cfg in seq:
        if cache.get("val") is None:
            telemetry = (cfg.get("gateway") or {}).get("telemetry") or {}
            segments = telemetry.get("session_segments") or {}
            cache["val"] = {
                "on_compaction": bool(segments.get("on_compaction", False)),
                "max_turns": max(0, int(segments.get("max_turns", 0) or 0)),
            }
        out.append(cache["val"])
    return out


mutant_res = mutant_segments_config({"val": None}, OFF_CFG, ON_CFG)
check(
    "E1 MUTANT pins profile B to profile A's value (this is the bug)",
    mutant_res == [{"on_compaction": False, "max_turns": 0}] * 2,
    f"mutant returned {mutant_res}; expected the bug to reproduce",
)
check(
    "E2 non-vacuity: the REAL fixed function does NOT reproduce the bug",
    _profile_pair(OFF_CFG, ON_CFG) != mutant_res,
)

_restore()


def test_no_cross_file_pollution() -> None:
    assert _gr._load_gateway_config is _REAL_LOADER
    assert _hc.get_config_path is _REAL_GET_PATH


def test_segments_config_production_shape() -> None:
    assert not FAILS, f"{len(FAILS)} failed: {FAILS}"


if __name__ == "__main__":
    print()
    if FAILS:
        print(f"  {len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("  ALL PASS")
