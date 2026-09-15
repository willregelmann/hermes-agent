"""Runtime readers in cli.py must resolve HERMES_HOME LIVE, not from the
module-body import-time snapshot.

THE DEFECT (issue #18, follow-up to #11)
PR #11 moved ``load_cli_config()`` off the module-body constant
``_hermes_home`` and onto ``get_hermes_home()``. Every OTHER reader of that
constant was left behind: ``show_config()`` printed the config path,
``_load_prefill_messages()`` resolved a relative prefill file, and
``HermesCLI._history_file`` picked the history file, all from the frozen
snapshot. Before #11 all of them agreed on the wrong profile -- wrong but
coherent. After #11 ``/config`` prints the path of a file the running config
did not come from, and the prefill loader silently reads another profile's
file.

THE RULE (cache-scoping rider B): key and value must come from the same
resolver. The constant itself is not deleted -- it is CORRECT for the one
import-time job it has (the dotenv load, which must run once at import,
before any profile scope exists) -- so it is renamed
``_IMPORT_TIME_HERMES_HOME`` to say what it is, and every runtime reader is
moved onto ``get_hermes_home()``.

DECLARED SUBJECT (lesson 49a): interpreter AND tree are stamped into the
report and prereq-probed before any case runs.

CONTROL DISCIPLINE: the scoped measurement is paired with an unscoped control
that must yield profile A throughout, so a pass cannot be an accident of the
fixture; and each behavioural claim is paired with a mutant that restores the
snapshot read at exactly that site.

SCOPE LIMIT, STATED NOT PAPERED OVER: D1 (show_config) and D2
(_load_prefill_messages) are exercised behaviourally in a real child
interpreter. ``HermesCLI._history_file`` and the remaining sites (paste dir,
interrupt debug log, onboarding mark_seen) are assignments inside a
constructor / deep UI callbacks that cannot be reached without standing up a
full console, so they are covered ONLY by case E2, a whole-class invariant on
the source: no runtime site may reference the import-time constant.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  :: {detail}")


# ── DECLARED SUBJECT ──────────────────────────────────────────────────────
TREE = os.environ.get("HERMES_TREE") or str(Path(__file__).resolve().parents[1])
PY = os.environ.get("HERMES_PY") or sys.executable


def _head(tree: str) -> str:
    try:
        r = subprocess.run(["git", "-C", tree, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() or "<no-git>"
    except Exception:
        return "<no-git>"


print("SUBJECT (declared, not inferred)")
print(f"  tree        : {TREE}")
print(f"  tree HEAD   : {_head(TREE)}")
print(f"  interpreter : {PY}")

check("S1 the declared tree contains cli.py", os.path.exists(os.path.join(TREE, "cli.py")),
      f"{TREE}/cli.py missing -- every case below would measure the wrong thing")

_pre = subprocess.run([PY, "-c", "import prompt_toolkit, yaml; print('ok')"],
                      capture_output=True, text=True,
                      env={**os.environ, "PYTHONPATH": TREE}, timeout=120)
check("S2 the interpreter can import cli.py's import-time deps", "ok" in _pre.stdout,
      f"prereq failed: {(_pre.stderr or '')[-200:]} -- a child dying at import "
      f"would render as a clean boolean")


# ── FIXTURE: two homes with distinguishable config + prefill files ────────
HOME_A = tempfile.mkdtemp(prefix="clihome-A-")
HOME_B = tempfile.mkdtemp(prefix="clihome-B-")
for h, marker in ((HOME_A, "PROFILE-A"), (HOME_B, "PROFILE-B")):
    Path(h, "config.yaml").write_text("model:\n  default: %s\n" % marker, encoding="utf-8")
    Path(h, "prefill.json").write_text(
        json.dumps([{"role": "user", "content": marker}]), encoding="utf-8")

PROBE = """
    import os, json
    os.environ["HERMES_HOME"] = %(A)r
    from hermes_constants import (get_hermes_home, set_hermes_home_override,
                                  reset_hermes_home_override)

    tok = set_hermes_home_override(%(B)r)
    inside = str(get_hermes_home())
    import cli                      # FIRST import, inside profile %(WHICH)s
    reset_hermes_home_override(tok)
    after = str(get_hermes_home())

    print("inside:", inside)
    print("after:", after)
    print("snapshot:", str(getattr(cli, "_IMPORT_TIME_HERMES_HOME",
                                   getattr(cli, "_hermes_home", "<NEITHER>"))))
    print("loaded_from:", str(cli.load_cli_config().get("model", {}).get("default")))

    # D2 -- relative prefill path, resolved against whichever home wins
    pf = cli._load_prefill_messages("prefill.json")
    print("prefill:", (pf[0]["content"] if pf else "<EMPTY>"))

    # D1 -- show_config's displayed config path, captured from real stdout
    import io, contextlib, datetime, types
    fake = types.SimpleNamespace(
        console=None, api_key="sk-fakefakefake", agent=None,
        model="m", base_url="u", max_turns=1, enabled_toolsets=[],
        verbose=False, session_start=datetime.datetime.now(),
    )
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            cli.HermesCLI.show_config(fake)
        line = [l for l in buf.getvalue().splitlines() if "Config File:" in l]
        # the line is "  Config File: <path> (loaded)" -- keep the path only
        print("displayed:", line[0].split("Config File:", 1)[1].strip().split(" (")[0] if line else "<NOLINE>")
    except Exception as e:
        print("displayed: <RAISED %%s: %%s>" %% (type(e).__name__, e))
"""


def run(code: str, tree: str) -> tuple[int, str]:
    env = {**os.environ, "PYTHONPATH": tree}
    env.pop("HERMES_HOME", None)
    p = subprocess.run([PY, "-c", textwrap.dedent(code)], capture_output=True,
                       text=True, env=env, cwd=tree, timeout=240)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def field(text: str, k: str) -> str:
    for line in text.splitlines():
        if line.startswith(k + ":"):
            return line.split(":", 1)[1].strip()
    return "<MISSING>"


SCOPED = PROBE % {"A": HOME_A, "B": HOME_B, "WHICH": "B"}
UNSCOPED = PROBE % {"A": HOME_A, "B": HOME_A, "WHICH": "A (control)"}

print("\nD. THE FIX -- imported inside profile B, running in profile A")
rc, out = run(SCOPED, TREE)
inside, after = field(out, "inside"), field(out, "after")
snapshot = field(out, "snapshot")
loaded_from, prefill = field(out, "loaded_from"), field(out, "prefill")
displayed = field(out, "displayed")

print(f"    scope entered   : {inside}")
print(f"    scope exited to : {after}")
print(f"    import snapshot : {snapshot}")
print(f"    load_cli_config : {loaded_from}")
print(f"    prefill content : {prefill}")
print(f"    displayed path  : {displayed}")

check("D0 the child ran (no import death folded into a boolean)",
      rc == 0 and inside != "<MISSING>", f"rc={rc} out={out[-400:]}")
check("D1a the scope really entered profile B", inside.rstrip("/") == HOME_B.rstrip("/"),
      f"inside={inside}")
check("D1b the scope really exited to profile A", after.rstrip("/") == HOME_A.rstrip("/"),
      f"after={after}")
check("D2 the import-time constant IS still frozen to B (it is not supposed to move)",
      snapshot.rstrip("/") == HOME_B.rstrip("/"),
      f"snapshot={snapshot} -- if this moved, the rename deleted a real behaviour")
check("D3 load_cli_config still resolves live (PR #11 not regressed)",
      loaded_from == "PROFILE-A", f"got {loaded_from!r}")
check("D4 show_config displays profile A's config.yaml, the file the config came from",
      displayed.rstrip("/") == str(Path(HOME_A, "config.yaml")),
      f"displayed={displayed!r}; pre-fix this is B's path while D3 says A")
check("D5 _load_prefill_messages reads profile A's prefill file",
      prefill == "PROFILE-A", f"got {prefill!r}; pre-fix this is 'PROFILE-B'")

print("\nE. CONTROL -- no cross-profile scope: every reader must say A")
rc_c, out_c = run(UNSCOPED, TREE)
check("E0 the control child ran independently",
      rc_c == 0 and field(out_c, "inside") != "<MISSING>", f"rc={rc_c} out={out_c[-400:]}")
check("E1a control snapshot is A (so D2's B was caused by the scope)",
      field(out_c, "snapshot").rstrip("/") == HOME_A.rstrip("/"),
      f"ctl snapshot={field(out_c, 'snapshot')}")
check("E1b control displays A", field(out_c, "displayed").rstrip("/") == str(Path(HOME_A, "config.yaml")),
      f"ctl displayed={field(out_c, 'displayed')!r} -- D4 must not pass for an unrelated reason")
check("E1c control prefill is A", field(out_c, "prefill") == "PROFILE-A",
      f"ctl prefill={field(out_c, 'prefill')!r}")

SRC = Path(TREE, "cli.py").read_text(encoding="utf-8")

print("\nE2. WHOLE-CLASS INVARIANT -- no runtime site reads the import-time constant")
code_lines = [l for l in SRC.splitlines() if not l.lstrip().startswith("#")]
hits = [l.strip() for l in code_lines
        if re.search(r"(?<![\w.])_IMPORT_TIME_HERMES_HOME(?![\w])", l)]
for h in hits:
    print(f"    {h}")
check("E2a the constant still exists and is still assigned once",
      sum(1 for h in hits if h.startswith("_IMPORT_TIME_HERMES_HOME =")) == 1,
      f"hits={hits}")
check("E2b its ONLY other use is the import-time dotenv load",
      len(hits) == 2 and "load_hermes_dotenv(" in hits[1],
      f"a runtime reader is still on the snapshot: {hits}")
check("E2c the old name is gone entirely (no reader left behind under it)",
      not re.search(r"(?<![\w.])_hermes_home(?![\w])", SRC),
      "a bare `_hermes_home` survives -- the rename was partial")

print("\nF. MUTANTS -- restore the snapshot read, one site at a time")


def mutate(anchor: str, replacement: str, label: str, expect_field: str, expect_value: str):
    n = SRC.count(anchor)
    check(f"{label}0 the mutation anchor matches EXACTLY ONCE", n == 1,
          f"anchor appears {n} times -- VACUOUS ANCHOR, mutant proves nothing")
    if n != 1:
        return
    mut_dir = tempfile.mkdtemp(prefix="clihome-mut-")
    for entry in os.listdir(TREE):
        if entry in ("cli.py", "__pycache__"):
            continue
        try:
            os.symlink(os.path.join(TREE, entry), os.path.join(mut_dir, entry))
        except OSError:
            pass
    Path(mut_dir, "cli.py").write_text(SRC.replace(anchor, replacement, 1), encoding="utf-8")
    rc_m, out_m = run(SCOPED, mut_dir)
    got = field(out_m, expect_field)
    print(f"    {label} mutant {expect_field}: {got}")
    check(f"{label}1 the mutant child ran (not an unapplied edit, not an import death)",
          rc_m == 0 and got != "<MISSING>", f"rc={rc_m} out={out_m[-400:]}")
    check(f"{label}2 MUTANT reproduces the frozen-profile-B read",
          got == expect_value, f"mutant produced {got!r}, expected {expect_value!r}")


mutate("        user_config_path = get_hermes_home() / 'config.yaml'\n",
       "        user_config_path = _IMPORT_TIME_HERMES_HOME / 'config.yaml'\n",
       "F1 (show_config)", "displayed", str(Path(HOME_B, "config.yaml")))

mutate("        path = get_hermes_home() / path\n",
       "        path = _IMPORT_TIME_HERMES_HOME / path\n",
       "F2 (_load_prefill_messages)", "prefill", "PROFILE-B")


def test_cli_home_frozen_readers() -> None:
    """pytest entry point -- the cases run at import."""
    assert not FAILS, f"{len(FAILS)} failed: {FAILS}"


if __name__ == "__main__":
    print()
    if FAILS:
        print(f"  {len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("  ALL PASS")
