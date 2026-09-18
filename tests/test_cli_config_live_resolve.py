"""load_cli_config() must read the LIVE home, not a module-body snapshot.

THE DEFECT
``cli.py`` assigns an import-time home constant in its module body (named
``_hermes_home`` when this suite was written, renamed ``_IMPORT_TIME_HERMES_HOME``
by the #18 follow-up) and ``load_cli_config()`` read that constant. Nothing in the gateway imports ``cli``
eagerly — every reference is a function-local lazy import — and on a multiplexed
gateway ~30 of those call sites sit inside ``_profile_runtime_scope``. So the
FIRST lazy import freezes the snapshot to whichever profile happened to be
mid-turn, and every later call returns that profile's config regardless of the
live home.

WHY IT MATTERS RATHER THAN BEING COSMETIC: there are live re-callers.
``cli.py`` calls ``load_cli_config()`` again at the approval gates, and
``hermes_cli/commands.py`` calls it for the personalities memo. Those sites
re-read deliberately, and before this fix they re-read a frozen constant.

DECLARED SUBJECT (lesson 49a): the interpreter AND the tree are stamped into
the report, and both are prereq-probed before any case runs. A wrong-but-
importable tree is the silent version of the wrong-interpreter bug.

CONTROL DISCIPLINE (lesson 49a): every case that asserts a negative has an arm
that can fail independently. A control that dies the same death as its subject
is not a control.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

FAILS: list[str] = []
_RAN = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _RAN[0] += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  :: {detail}")


# ── DECLARED SUBJECT ──────────────────────────────────────────────────────
TREE = os.environ.get("HERMES_TREE") or str(Path(__file__).resolve().parents[1])
PY = os.environ.get("HERMES_PY") or sys.executable


def _head(tree: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", tree, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout.strip() or "<no-git>"
    except Exception:
        return "<no-git>"


print("SUBJECT (declared, not inferred)")
print(f"  tree        : {TREE}")
print(f"  tree HEAD   : {_head(TREE)}")
print(f"  interpreter : {PY}")

check("S1 the declared tree contains cli.py", os.path.exists(os.path.join(TREE, "cli.py")),
      f"{TREE}/cli.py missing — every case below would measure the wrong thing")

_pre = subprocess.run(
    [PY, "-c", "import prompt_toolkit, yaml; print('ok')"],
    capture_output=True, text=True, env={**os.environ, "PYTHONPATH": TREE}, timeout=120,
)
check("S2 the interpreter can import cli.py's import-time deps",
      "ok" in _pre.stdout,
      f"prereq failed: {(_pre.stderr or '')[-200:]} — a child dying at import "
      f"would render as a clean boolean")


def child(code: str) -> tuple[int, str]:
    env = {**os.environ, "PYTHONPATH": TREE}
    env.pop("HERMES_HOME", None)
    p = subprocess.run([PY, "-c", textwrap.dedent(code)], capture_output=True,
                       text=True, env=env, cwd=TREE, timeout=180)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def field(text: str, k: str) -> str:
    for line in text.splitlines():
        if line.startswith(k + ":"):
            return line.split(":", 1)[1].strip()
    return "<MISSING>"


# Two homes with distinguishable configs.
HOME_A = tempfile.mkdtemp(prefix="clilive-A-")
HOME_B = tempfile.mkdtemp(prefix="clilive-B-")
for h, marker in ((HOME_A, "PROFILE-A"), (HOME_B, "PROFILE-B")):
    Path(h, "config.yaml").write_text(
        "model:\n  default: %s\n" % marker, encoding="utf-8"
    )

SCOPED = """
    import os
    os.environ["HERMES_HOME"] = %(A)r
    from hermes_constants import (get_hermes_home, set_hermes_home_override,
                                  reset_hermes_home_override)

    tok = set_hermes_home_override(%(B)r)
    inside = str(get_hermes_home())
    import cli                      # first import, inside profile B
    reset_hermes_home_override(tok)
    after = str(get_hermes_home())

    print("inside:", inside)
    print("after:", after)
    print("snapshot:", str(getattr(cli, "_IMPORT_TIME_HERMES_HOME",
                                   getattr(cli, "_hermes_home", "<NEITHER>"))))
    print("reload_model:", str(cli.load_cli_config().get("model", {}).get("default")))
""" % {"A": HOME_A, "B": HOME_B}

print("\nA. THE FIX — a re-call after the scope exits reads the LIVE home")

rc, out = child(SCOPED)
inside, after = field(out, "inside"), field(out, "after")
snapshot, reload_model = field(out, "snapshot"), field(out, "reload_model")

print(f"    scope entered   : {inside}")
print(f"    scope exited to : {after}")
print(f"    module snapshot : {snapshot}")
print(f"    load_cli_config : model.default = {reload_model}")

check("A0 the child ran (no import death folded into a boolean)",
      rc == 0 and inside != "<MISSING>", f"rc={rc} out={out[-300:]}")
check("A1 the scope really entered profile B",
      inside.rstrip("/") == HOME_B.rstrip("/"), f"inside={inside}")
check("A2 the scope really exited to profile A",
      after.rstrip("/") == HOME_A.rstrip("/"), f"after={after}")
# RE-POLARISED 2026-09-15 (Wren, #21).  This case used to assert
# ``snapshot == HOME_B`` — "the defect is still present" — which was true when
# #19 shipped, because #19 only fixed the READER and deliberately left the
# import-time constant frozen to whoever imported first.  That constant is not
# inert: it hydrates ``.env`` into the process environment and (via #20) it is
# the value the one-shot env bridge exports.  So "frozen to whichever profile
# was mid-turn" was still a live cross-profile leak, one layer below the one
# #19 closed.  The constant now resolves ``get_process_hermes_home()``, which
# ignores the context-local override by construction.
#
# A finding belongs in the journal; a CONTRACT belongs in a test.  The contract
# is: the import-time constant is the PROCESS home, whoever imported first.
check("A3 the import-time snapshot is the PROCESS home, not the importing scope",
      snapshot.rstrip("/") == HOME_A.rstrip("/"),
      f"snapshot={snapshot} — imported inside profile B and froze to it; "
      f"that constant feeds load_hermes_dotenv() and the os.environ bridge")
check("A4 load_cli_config() returns profile A's config, not the frozen B",
      reload_model == "PROFILE-A",
      f"got {reload_model!r}; pre-fix this returns 'PROFILE-B'")

print("\nB. CONTROL — no scope: the same sequence must yield A throughout")

rc, out_ctl = child(SCOPED.replace(
    "set_hermes_home_override(%r)" % HOME_B,
    "set_hermes_home_override(%r)" % HOME_A,
))
ctl_model = field(out_ctl, "reload_model")
ctl_snapshot = field(out_ctl, "snapshot")
print(f"    control snapshot: {ctl_snapshot}")
print(f"    control model   : {ctl_model}")
check("B0 the control child ran independently", rc == 0 and ctl_model != "<MISSING>",
      f"rc={rc} out={out_ctl[-300:]}")
check("B1 control snapshot is A (so B's snapshot in A3 was caused by the scope)",
      ctl_snapshot.rstrip("/") == HOME_A.rstrip("/"), f"ctl_snapshot={ctl_snapshot}")
check("B2 control also resolves A (A4 is not passing for an unrelated reason)",
      ctl_model == "PROFILE-A", f"ctl_model={ctl_model!r}")

print("\nC. MUTANT — restore the snapshot read; A4 must die")

src = Path(TREE, "cli.py").read_text(encoding="utf-8")
# The import-time constant's NAME is not the subject of this suite; derive it
# from the tree so a rename cannot silently make the mutant unapplied.
SNAP_NAME = ("_IMPORT_TIME_HERMES_HOME" if "_IMPORT_TIME_HERMES_HOME =" in src
             else "_hermes_home")
# TWO anchors, because there are now two halves to the fix and a mutant that
# reverts only one is not a mutant: with the snapshot pinned to the process
# home, reverting the reader alone still yields PROFILE-A and the case dies
# for the wrong reason (observed 2026-09-15).  A mutant is only evidence if it
# differs from the current subject by exactly the thing you mutated.
ANCHOR = "    user_config_path = (home or get_hermes_home()) / 'config.yaml'"
ANCHOR_SNAP = "_IMPORT_TIME_HERMES_HOME = get_process_hermes_home()"
check("C0 the reader mutation anchor exists", ANCHOR in src,
      "the fix is not where this suite thinks it is — every case above is suspect")
check("C0b the snapshot mutation anchor exists", ANCHOR_SNAP in src,
      "the import-time constant no longer resolves the process home — "
      "A3's contract is unenforceable")

if ANCHOR in src and ANCHOR_SNAP in src:
    mut_dir = tempfile.mkdtemp(prefix="clilive-mut-")
    # Mirror the tree by symlink, overriding only cli.py.
    for entry in os.listdir(TREE):
        if entry == "cli.py":
            continue
        try:
            os.symlink(os.path.join(TREE, entry), os.path.join(mut_dir, entry))
        except OSError:
            pass
    mutated = src.replace(
        ANCHOR, "    user_config_path = %s / 'config.yaml'" % SNAP_NAME, 1)
    # revert the snapshot too — this is the pre-#21 world
    mutated = mutated.replace(
        ANCHOR_SNAP, "_IMPORT_TIME_HERMES_HOME = get_hermes_home()", 1)
    Path(mut_dir, "cli.py").write_text(mutated, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": mut_dir}
    env.pop("HERMES_HOME", None)
    p = subprocess.run([PY, "-c", textwrap.dedent(SCOPED)], capture_output=True,
                       text=True, env=env, cwd=mut_dir, timeout=180)
    mut_out = (p.stdout or "") + (p.stderr or "")
    mut_model = field(mut_out, "reload_model")
    print(f"    mutant model    : {mut_model}")
    check("C1 the mutant child ran (not an unapplied edit, not an import death)",
          p.returncode == 0 and mut_model != "<MISSING>",
          f"rc={p.returncode} out={mut_out[-300:]}")
    check("C2 MUTANT returns the FROZEN profile B config (killed by A4)",
          mut_model == "PROFILE-B",
          f"mutant returned {mut_model!r}; expected the pre-fix behaviour")
    MUT_DIR = mut_dir
else:
    MUT_DIR = None


print("\nD. THE ENV BRIDGE — importing inside profile B must not export B's "
      "settings into the shared environment")

# The import-time bridge (_export_config_to_env, #20) writes ~20 values into
# process-global os.environ and they OUTLIVE the scope that was active when the
# import happened.  HERMES_SEARCH_SLOW_MS is the cheapest observable of that
# bridge: it is a plain scalar copied straight out of sessions.search_slow_ms.
for _h, _v in ((HOME_A, 111), (HOME_B, 999)):
    _p = Path(_h, "config.yaml")
    _p.write_text(_p.read_text(encoding="utf-8")
                  + "sessions:\n  search_slow_ms: %d\n" % _v, encoding="utf-8")

BRIDGE = """
    import os
    os.environ["HERMES_HOME"] = %(A)r
    os.environ.pop("HERMES_SEARCH_SLOW_MS", None)
    from hermes_constants import (set_hermes_home_override,
                                  reset_hermes_home_override)
    tok = set_hermes_home_override(%(B)r)
    import cli                      # first import, inside profile B
    reset_hermes_home_override(tok)
    print("slow_ms:", os.environ.get("HERMES_SEARCH_SLOW_MS"))
""" % {"A": HOME_A, "B": HOME_B}

rc, out_d = child(BRIDGE)
d_slow = field(out_d, "slow_ms")
print(f"    exported slow_ms: {d_slow}")
check("D0 the bridge child ran and the bridge fired at all (non-vacuity)",
      rc == 0 and d_slow not in ("<MISSING>", "None"),
      f"rc={rc} slow_ms={d_slow!r} out={out_d[-300:]} — an unset value here "
      f"means the export never ran, so D1 would pass for free")
check("D1 the exported value is profile A's (the process home), not B's",
      d_slow == "111",
      f"got {d_slow!r}; '999' means profile B's setting leaked into the "
      f"shared environment and outlived its scope")


print("\nE. MUTANT FOR D — the pre-#21 snapshot must leak B's setting")

if MUT_DIR is None:
    check("E0 a mutant tree was built (else D1 has no kill)", False,
          "C section did not build a mutant; D1 is unfalsified")
else:
    _env = {**os.environ, "PYTHONPATH": MUT_DIR}
    _env.pop("HERMES_HOME", None)
    _p = subprocess.run([PY, "-c", textwrap.dedent(BRIDGE)], capture_output=True,
                        text=True, env=_env, cwd=MUT_DIR, timeout=180)
    _o = (_p.stdout or "") + (_p.stderr or "")
    e_slow = field(_o, "slow_ms")
    print(f"    mutant exported slow_ms: {e_slow}")
    check("E0 the mutant bridge child ran", _p.returncode == 0 and e_slow != "<MISSING>",
          f"rc={_p.returncode} out={_o[-300:]}")
    check("E1 MUTANT exports profile B's 999 (so D1 has a real kill)",
          e_slow == "999",
          f"mutant exported {e_slow!r}; if this is 111 the mutation did not "
          f"reach the bridge and D1 proves nothing")



# ── F. THE LIVE READER, READ FROM INSIDE THE SCOPE ────────────────────────
# Everything above measures load_cli_config() AFTER the scope exits, where the
# live home and the PROCESS home are the same value (A).  So the whole suite is
# satisfied by a reader that resolves get_process_hermes_home(), or that returns
# the module-global CLI_CONFIG, or that memoises its first answer -- three
# spellings of the exact defect #11 exists to remove, none of them observable
# from outside a scope.  (Lesson 82: enumerate every EXPRESSION that produces
# the forbidden value; the process resolver is the spelling nobody arms.)
print("\nF. THE LIVE READER — a call INSIDE profile B's scope must return B")

LIVE = """
    import os
    os.environ["HERMES_HOME"] = %(A)r
    from hermes_constants import (set_hermes_home_override,
                                  reset_hermes_home_override)
    import cli                      # imported OUTSIDE any scope: home A owns it
    tok = set_hermes_home_override(%(B)r)
    print("inside_model:", str(cli.load_cli_config().get("model", {}).get("default")))
    reset_hermes_home_override(tok)
    print("after_model:", str(cli.load_cli_config().get("model", {}).get("default")))
""" % {"A": HOME_A, "B": HOME_B}

rc, out_f = child(LIVE)
f_in, f_after = field(out_f, "inside_model"), field(out_f, "after_model")
print(f"    inside scope B  : {f_in}")
print(f"    after exit to A : {f_after}")
check("F0 the live-read child ran", rc == 0 and f_in != "<MISSING>",
      f"rc={rc} out={out_f[-300:]}")
check("F1 load_cli_config() INSIDE profile B returns B's config",
      f_in == "PROFILE-B",
      f"got {f_in!r}; 'PROFILE-A' means the reader resolved the PROCESS home "
      f"(or returned the frozen module global) and ignored the live scope — "
      f"the #11 defect in a spelling every other case is blind to")
check("F2 the SAME reader returns A again after the scope exits (not memoised)",
      f_after == "PROFILE-A",
      f"got {f_after!r}; a reader that caches its first answer passes F1 and "
      f"is still frozen")

# ── G. THE PURITY CLAIM ───────────────────────────────────────────────────
# load_cli_config()'s docstring states it is a PURE READ and that the env bridge
# was moved out for exactly this reason: calling it from inside another
# profile's scope must not leave that profile's settings in the shared
# environment.  That sentence had no arm (lesson 73: a claim in the prose is a
# documented invariant with no binding).
print("\nG. PURITY — a scoped load_cli_config() must not write to os.environ")

PURE = """
    import os
    os.environ["HERMES_HOME"] = %(A)r
    os.environ.pop("HERMES_SEARCH_SLOW_MS", None)
    from hermes_constants import (set_hermes_home_override,
                                  reset_hermes_home_override)
    import cli
    print("import_slow:", os.environ.get("HERMES_SEARCH_SLOW_MS"))
    tok = set_hermes_home_override(%(B)r)
    _cfg = cli.load_cli_config()
    print("scoped_cfg_slow:", str(_cfg.get("sessions", {}).get("search_slow_ms")))
    reset_hermes_home_override(tok)
    print("env_slow:", os.environ.get("HERMES_SEARCH_SLOW_MS"))
""" % {"A": HOME_A, "B": HOME_B}

rc, out_g = child(PURE)
g_import, g_cfg, g_env = (field(out_g, "import_slow"), field(out_g, "scoped_cfg_slow"),
                          field(out_g, "env_slow"))
print(f"    import-time export : {g_import}")
print(f"    scoped read value  : {g_cfg}")
print(f"    env after the call : {g_env}")
check("G0 the child ran and the import-time bridge fired",
      rc == 0 and g_import == "111",
      f"rc={rc} import_slow={g_import!r} out={out_g[-300:]}")
check("G1 the scoped call really read B (so there was something to leak)",
      g_cfg == "999",
      f"got {g_cfg!r}; if this is 111 the reader never saw B and G2 passes "
      f"for free")
check("G2 the scoped call left the shared environment on A's value",
      g_env == "111",
      f"got {g_env!r}; '999' means load_cli_config() is not the pure read its "
      f"docstring claims and re-exported B into the process environment")

# ── H. THE SCOPE-IMPORT WARNING ───────────────────────────────────────────
# The pin at the bottom of cli.py makes a scoped import HARMLESS; the warning
# beside it is what makes it VISIBLE.  Its only observable effect is a log line,
# so no verdict-reading case can see it deleted (lesson 66).
print("\nH. THE SCOPE-IMPORT WARNING — 'harmless' must still be 'visible'")

WARN_PHRASE = "imported inside a profile scope"
WARNED = """
    import logging, sys
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    import os
    os.environ["HERMES_HOME"] = %(A)r
    from hermes_constants import (set_hermes_home_override,
                                  reset_hermes_home_override)
    tok = set_hermes_home_override(%(B)r)
    import cli
    reset_hermes_home_override(tok)
    print("imported_ok: 1")
"""

rc, out_h = child(WARNED % {"A": HOME_A, "B": HOME_B})
check("H0 the scoped-import child ran", rc == 0 and field(out_h, "imported_ok") == "1",
      f"rc={rc} out={out_h[-300:]}")
check("H1 importing inside a foreign scope is WARNED about",
      WARN_PHRASE in out_h,
      "the pin makes a scoped import harmless; without the warning it is also "
      "invisible, and nobody learns that a call path reaches cli.py from "
      "inside a scope")

rc, out_h2 = child(WARNED % {"A": HOME_A, "B": HOME_A})
check("H2 the control child ran", rc == 0 and field(out_h2, "imported_ok") == "1",
      f"rc={rc} out={out_h2[-300:]}")
check("H3 an import with NO foreign scope does not warn (H1 is not vacuous)",
      WARN_PHRASE not in out_h2,
      "the warning fires even when the import home IS the process home — it "
      "carries no information")

# ── CASE FLOOR ───────────────────────────────────────────────────────────
# A script suite can die halfway and a fail-counting reader calls that a pass
# (lesson 77).  Floor set from the MEASURED count AFTER the run, and it counts
# itself (lesson 86c).
_CASES = 28
check(f"Z1 case floor: at least {_CASES} cases ran",
      (len(FAILS) + _RAN[0]) >= _CASES,
      f"only {len(FAILS) + _RAN[0]} cases observed; the suite aborted early")


def test_cli_config_live_resolve() -> None:
    """pytest entry point — the cases run at import."""
    assert not FAILS, f"{len(FAILS)} failed: {FAILS}"


if __name__ == "__main__":
    print()
    if FAILS:
        print(f"  {len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("  ALL PASS")
