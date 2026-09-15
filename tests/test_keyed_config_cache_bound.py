"""The two keyed config caches must be BOUNDED and must LOAD ONCE.

SUBJECTS (both, because the defect is shared and copying the key shape is
exactly how it spread):
  agent/turn_context.py::_recall_indicator_config  / _RECALL_CFG_CACHE
  agent/relay_runtime.py::_segments_config         / _SEGMENTS_CONFIG_CACHE

WHY THIS SUITE EXISTS. Both caches were bare unkeyed module globals that let
the first profile pin the value for every other profile (fixed 2026-09-13 and
2026-09-15). The fix keyed them by ``(str(path), st_mtime_ns, st_size)``,
citing ``agent/skill_utils.py::_load_raw_config`` as the in-tree precedent.
The key shape was copied; two properties of that precedent were not.

  1. THE BOUND. ``_load_raw_config`` calls ``_RAW_CONFIG_CACHE.clear()``
     immediately before inserting, so it holds ONE entry. Putting
     ``st_mtime_ns`` in the KEY instead means every config.yaml EDIT adds a
     permanent entry rather than replacing one — correct answers, unbounded
     map. Fixed by keying on the PATH and moving the freshness stamp into the
     VALUE, which is the same staleness semantics with a bound.

  2. LOAD-ONCE. Before keying, ``_segments_config`` was double-checked
     locking: the loader ran INSIDE the critical section, so N concurrent
     first-callers produced one read of config.yaml. Both keyed versions took
     the lock only to STORE, so N concurrent first-callers each load and the
     last write wins. Idempotent — hence invisible — but load-once was a real
     property and keying silently dropped it.

CONTRACTS, not snapshots: "one entry per config file regardless of edit
count" and "one loader call per (file, stamp) under concurrency". Both mutant
arms re-implement the shipped-but-wrong shape and must DIFFER from the
subject; each has a non-vacuity arm asserting the real function does not
reproduce the mutant's observable.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import threading
import time

# TREE, not a fixed relative path: MUTANT ZERO is the real pre-fix tree, so
# the suite must be able to point at a different checkout of the same files.
# Defaults to the tree this file lives in.
TREE = os.environ.get("HERMES_TREE") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
sys.path.insert(0, TREE)

import agent.relay_runtime as rr  # noqa: E402
import agent.turn_context as tc  # noqa: E402
import gateway.run as _gr  # noqa: E402
import hermes_cli.config as _hc  # noqa: E402

# Name the subject out loud: a suite that can be pointed elsewhere must say
# where it was pointed, or a green run is green about an unknown tree.
print(f"SUBJECT TREE: {TREE}")
assert os.path.dirname(os.path.dirname(os.path.abspath(tc.__file__))) == os.path.realpath(
    TREE
), f"imported turn_context from {tc.__file__}, not from {TREE}"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  :: {detail}")


_REAL_LOADER = _gr._load_gateway_config
_REAL_GET_PATH = _hc.get_config_path

# (label, callable, cache dict, reset) — run every case against BOTH subjects.
SUBJECTS = [
    ("recall", lambda: tc._recall_indicator_config(), tc._RECALL_CFG_CACHE,
     tc._recall_indicator_cache_clear),
    ("segments", lambda: rr._segments_config(), rr._SEGMENTS_CONFIG_CACHE,
     rr._reset_segments_config_for_tests),
]


def _restore() -> None:
    _gr._load_gateway_config = _REAL_LOADER
    _hc.get_config_path = _REAL_GET_PATH
    for _, _, _, reset in SUBJECTS:
        reset()


def _write(path: pathlib.Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    # st_mtime_ns granularity here is fine, but force a distinct stamp so the
    # test is measuring the cache and not the filesystem clock.
    time.sleep(0.01)


# ---------------------------------------------------------------------------
print("A. THE BOUND — N edits of ONE config file leave ONE entry")

for label, subject, cache, reset in SUBJECTS:
    reset()
    home = tempfile.mkdtemp(prefix=f"bound-{label}-")
    cfgfile = pathlib.Path(home, "config.yaml")
    _write(cfgfile, "display: {}\n")
    _hc.get_config_path = lambda p=cfgfile: p
    loads = {"n": 0}

    def fake_load(loads=loads):
        loads["n"] += 1
        return {"gateway": {"telemetry": {"session_segments": {"max_turns": loads["n"]}}},
                "display": {"memory_recall_indicator": True}}

    _gr._load_gateway_config = fake_load
    try:
        for i in range(6):
            _write(cfgfile, "display: {}\n" + ("#" * i) + "\n")
            subject()
        entries = len(cache)
        check(
            f"A1 ({label}) 6 edits leave exactly ONE cache entry",
            entries == 1,
            f"cache holds {entries} entries after 6 edits",
        )
        check(
            f"A2 ({label}) non-vacuity: each edit really was re-loaded",
            loads["n"] == 6,
            f"loader ran {loads['n']} times, expected 6",
        )
    finally:
        _restore()


# ---------------------------------------------------------------------------
print("\nB. STALENESS SURVIVES THE BOUND — an edited file is re-read")

for label, subject, cache, reset in SUBJECTS:
    reset()
    home = tempfile.mkdtemp(prefix=f"stale-{label}-")
    cfgfile = pathlib.Path(home, "config.yaml")
    _write(cfgfile, "display: {}\n")
    _hc.get_config_path = lambda p=cfgfile: p
    seq = [
        {"gateway": {"telemetry": {"session_segments": {"max_turns": 1}}},
         "display": {"memory_recall_indicator": True}},
        {"gateway": {"telemetry": {"session_segments": {"max_turns": 2}}},
         "display": {"memory_recall_indicator": False}},
    ]
    st = {"i": 0}

    def fake_load(seq=seq, st=st):
        cfg = seq[min(st["i"], len(seq) - 1)]
        st["i"] += 1
        return cfg

    _gr._load_gateway_config = fake_load
    try:
        first = subject()
        cached = subject()
        # CAPTURE THE COUNT HERE, not at assert time. The first version of
        # this case read st["i"] after the post-edit third call and therefore
        # measured 2 by construction -- a test that fails on a correct
        # subject because its measurement point drifted past the thing it
        # was measuring.
        loads_before_edit = st["i"]
        _write(cfgfile, "display: {}\n# edited\n")
        after = subject()
        check(
            f"B1 ({label}) an unchanged file is served from cache",
            cached == first and loads_before_edit == 1,
            f"loader ran {loads_before_edit} times for two identical reads",
        )
        check(
            f"B2 ({label}) an EDITED file is re-read, not served stale",
            after != first,
            f"got {after} both times; staleness lost",
        )
    finally:
        _restore()


# ---------------------------------------------------------------------------
print("\nC. KEYING IS NOT WEAKENED — two config paths stay separate")

for label, subject, cache, reset in SUBJECTS:
    reset()
    homes = [tempfile.mkdtemp(prefix=f"keyA-{label}-"),
             tempfile.mkdtemp(prefix=f"keyB-{label}-")]
    for h in homes:
        _write(pathlib.Path(h, "config.yaml"), "display: {}\n")
    st = {"i": 0}
    seq = [
        {"gateway": {"telemetry": {"session_segments": {"max_turns": 0}}},
         "display": {"memory_recall_indicator": False}},
        {"gateway": {"telemetry": {"session_segments": {"max_turns": 99}}},
         "display": {"memory_recall_indicator": True}},
    ]

    def fake_path(homes=homes, st=st):
        return pathlib.Path(homes[min(st["i"], 1)], "config.yaml")

    def fake_load(seq=seq, st=st):
        cfg = seq[min(st["i"], 1)]
        st["i"] += 1
        return cfg

    _hc.get_config_path = fake_path
    _gr._load_gateway_config = fake_load
    try:
        out = [subject(), subject()]
        check(
            f"C1 ({label}) profile B is not pinned by profile A",
            out[0] != out[1],
            f"both profiles got {out[0]}",
        )
        check(
            f"C2 ({label}) two paths produce two entries, not one",
            len(cache) == 2,
            f"cache holds {len(cache)} entries for two distinct config paths",
        )
    finally:
        _restore()


# ---------------------------------------------------------------------------
print("\nD. LOAD-ONCE — N concurrent first-callers read config.yaml once")

CONC = 8


def _concurrent_loads(subject, reset, cfgfile) -> int:
    """Return how many times the loader ran for CONC simultaneous cold calls."""
    reset()
    loads = {"n": 0}
    gate = threading.Barrier(CONC)

    def fake_load(loads=loads):
        loads["n"] += 1
        time.sleep(0.05)  # widen the window a store-only lock leaves open
        return {"gateway": {"telemetry": {"session_segments": {"max_turns": 7}}},
                "display": {"memory_recall_indicator": True}}

    _hc.get_config_path = lambda p=cfgfile: p
    _gr._load_gateway_config = fake_load

    def worker():
        gate.wait()
        subject()

    threads = [threading.Thread(target=worker) for _ in range(CONC)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return loads["n"]


for label, subject, cache, reset in SUBJECTS:
    home = tempfile.mkdtemp(prefix=f"conc-{label}-")
    cfgfile = pathlib.Path(home, "config.yaml")
    _write(cfgfile, "display: {}\n")
    try:
        n = _concurrent_loads(subject, reset, cfgfile)
        check(
            f"D1 ({label}) {CONC} concurrent cold callers -> ONE load",
            n == 1,
            f"loader ran {n} times",
        )
    finally:
        _restore()


# ---------------------------------------------------------------------------
print("\nE. MUTANT 1 — st_mtime_ns back in the KEY; A1 must die")


def mutant_mtime_in_key(cfgfile: pathlib.Path, edits: int) -> int:
    """The shipped-but-unbounded shape: stamp in the key, one entry per edit."""
    cache: dict = {}
    for i in range(edits):
        _write(cfgfile, "display: {}\n" + ("#" * i) + "\n")
        stt = cfgfile.stat()
        key = (str(cfgfile), stt.st_mtime_ns, stt.st_size)
        if key not in cache:
            cache[key] = {"max_turns": i}
    return len(cache)


home = tempfile.mkdtemp(prefix="mut1-")
mf = pathlib.Path(home, "config.yaml")
_write(mf, "display: {}\n")
mut_entries = mutant_mtime_in_key(mf, 6)
check(
    "E1 MUTANT grows one entry per edit (this is the bug)",
    mut_entries == 6,
    f"mutant holds {mut_entries} entries, expected 6",
)

# non-vacuity: the real subjects, same 6 edits, hold one entry (re-measured
# here rather than trusted from case A).
for label, subject, cache, reset in SUBJECTS:
    reset()
    home = tempfile.mkdtemp(prefix=f"mut1nv-{label}-")
    cfgfile = pathlib.Path(home, "config.yaml")
    _write(cfgfile, "display: {}\n")
    _hc.get_config_path = lambda p=cfgfile: p
    _gr._load_gateway_config = lambda: {
        "gateway": {"telemetry": {"session_segments": {"max_turns": 1}}},
        "display": {"memory_recall_indicator": True},
    }
    try:
        for i in range(6):
            _write(cfgfile, "display: {}\n" + ("#" * i) + "\n")
            subject()
        check(
            f"E2 ({label}) non-vacuity: the REAL cache does NOT grow per edit",
            len(cache) == 1 and len(cache) != mut_entries,
            f"real cache holds {len(cache)}",
        )
    finally:
        _restore()


# ---------------------------------------------------------------------------
print("\nF. MUTANT 2 — lock held only to STORE; D1 must die")


def mutant_store_only_lock() -> int:
    """The shipped-but-racy shape: load outside the lock, store inside it."""
    cache: dict = {}
    lock = threading.Lock()
    loads = {"n": 0}
    gate = threading.Barrier(CONC)

    def load():
        loads["n"] += 1
        time.sleep(0.05)
        return {"max_turns": 7}

    def subject():
        key = "one-path"
        hit = cache.get(key)
        if hit is not None:
            return hit
        val = load()
        with lock:
            cache[key] = val
        return val

    def worker():
        gate.wait()
        subject()

    threads = [threading.Thread(target=worker) for _ in range(CONC)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return loads["n"]


mut_loads = mutant_store_only_lock()
check(
    "F1 MUTANT loads once per concurrent caller (this is the regression)",
    mut_loads > 1,
    f"mutant loaded {mut_loads} times; expected more than 1",
)

for label, subject, cache, reset in SUBJECTS:
    home = tempfile.mkdtemp(prefix=f"mut2nv-{label}-")
    cfgfile = pathlib.Path(home, "config.yaml")
    _write(cfgfile, "display: {}\n")
    try:
        n = _concurrent_loads(subject, reset, cfgfile)
        check(
            f"F2 ({label}) non-vacuity: the REAL cache loads once, unlike the mutant",
            n == 1 and n != mut_loads,
            f"real loaded {n}, mutant loaded {mut_loads}",
        )
    finally:
        _restore()


# ---------------------------------------------------------------------------
def test_keyed_config_cache_bound() -> None:
    """pytest entry point -- the cases run at import."""
    assert not FAILS, f"{len(FAILS)} failed: {FAILS}"


print("\n  ALL PASS" if not FAILS else f"\n  {len(FAILS)} FAILED: {FAILS}")
if FAILS:
    sys.exit(1)
