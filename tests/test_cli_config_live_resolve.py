"""load_cli_config() must read the LIVE home, not a module-body snapshot.

THE DEFECT
``cli.py`` assigns ``_hermes_home = get_hermes_home()`` in its module body and
``load_cli_config()`` read that constant. Nothing in the gateway imports ``cli``
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
    print("snapshot:", str(cli._hermes_home))
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
check("A3 the module snapshot IS still frozen to B (defect still present)",
      snapshot.rstrip("/") == HOME_B.rstrip("/"),
      f"snapshot={snapshot} — if this changed, the fix touched the wrong thing")
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
ANCHOR = "    user_config_path = get_hermes_home() / 'config.yaml'"
check("C0 the mutation anchor exists", ANCHOR in src,
      "the fix is not where this suite thinks it is — every case above is suspect")

if ANCHOR in src:
    mut_dir = tempfile.mkdtemp(prefix="clilive-mut-")
    # Mirror the tree by symlink, overriding only cli.py.
    for entry in os.listdir(TREE):
        if entry == "cli.py":
            continue
        try:
            os.symlink(os.path.join(TREE, entry), os.path.join(mut_dir, entry))
        except OSError:
            pass
    Path(mut_dir, "cli.py").write_text(
        src.replace(ANCHOR, "    user_config_path = _hermes_home / 'config.yaml'", 1),
        encoding="utf-8",
    )
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


def test_cli_config_live_resolve() -> None:
    """pytest entry point — the cases run at import."""
    assert not FAILS, f"{len(FAILS)} failed: {FAILS}"


if __name__ == "__main__":
    print()
    if FAILS:
        print(f"  {len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("  ALL PASS")
