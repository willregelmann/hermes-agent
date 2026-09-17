"""Guard: this suite must never be green by collecting nothing.

WHY THIS FILE EXISTS
Ash hit exactly this while fixing the inert recall-indicator gate: his first
version put assertions at module scope with a sys.exit at the end. Pytest
imported the module, ran the body, and reported "no tests ran" — GREEN, while
gating nothing. Silence read as success, which is the same defect class as the
inert gate he was fixing, one level up.

test_handoff.py is deliberately a module-level script (it runs under bare
python3 with no dependencies, which is how it runs on this box — there is no
pytest in the system interpreter). That is a legitimate choice, but it means
a pytest run over tests/ collects ZERO test functions from it and says so in a
way that looks like success.

So this file provides the pytest-visible entry point. Under pytest it is a
real collected test; run directly it reports the same thing. Either way the
answer comes from EXECUTING the suite, not from importing it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SUITE = Path(__file__).with_name("test_handoff.py")


def _run() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(SUITE)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def test_handoff_suite_passes() -> None:
    """Run the script suite as a subprocess and require a real verdict.

    The case floor is the part that earns its keep. A mutant that makes a
    reader raise does not FAIL a case, it kills the run part-way through: no
    FAIL line is ever printed, so a harness reading exit codes sees "no
    failures". The floor turns a truncated run into a failure. Measured: with
    `_rows()` catching a narrower exception the script dies after 24 of 65
    cases, and this guard exits 1 where a fail-line count would have said zero.

    Asserting on the VERDICT TEXT as well as the exit code is deliberate: an
    empty or crashed run can exit 0 in some shells, and "no output" must not be
    mistaken for "no failures". The suite has to positively say ALL PASS.
    """
    code, out = _run()
    assert "ALL PASS" in out, f"suite did not report ALL PASS:\n{out[-2000:]}"
    assert code == 0, f"suite exited {code}:\n{out[-2000:]}"
    # Non-vacuity: a suite that ran no cases must not count as passing.
    assert out.count("PASS ") >= 60, (
        f"only {out.count('PASS ')} cases ran (expected >= 60) — the suite may have "
        f"collected nothing, or died part-way through:\n"
        f"{out[-2000:]}"
    )


if __name__ == "__main__":
    code, out = _run()
    print(out)
    ran = out.count("PASS ")
    print(f"[pytest-visibility guard] cases executed: {ran}")
    sys.exit(0 if ("ALL PASS" in out and code == 0 and ran >= 60) else 1)
