"""The large-runner lane list in ci.yaml must match reality.

WHY THIS TEST EXISTS
``all-checks-pass`` treats a skipped job as passing. That is correct for a
lane skipped because nothing in its area changed, and wrong for one skipped
because this account cannot run it -- the suite never executed, so a green
would assert coverage that does not exist. The gate therefore names the
large-runner lanes explicitly and downgrades them to a warning.

That list is a hardcoded literal inside a shell-embedded Python block, and a
hardcoded list of things that should be derived is exactly how
``notify_tools`` in background_review.py silently stopped covering new
memory providers. If a lane later switches to a large runner and nobody
updates the literal, it reads as a tick again and the false-green returns --
silently, which is the worst property a gate can have.

The gate job has no ``actions/checkout`` step, so it cannot read the
sub-workflow YAML at runtime to derive the list itself. Adding a checkout to
a job whose whole purpose is to be fast and dependency-free is a poor trade.
So the list stays literal, and THIS test is what keeps it honest: it derives
the truth from the workflow files and fails when the literal drifts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
CI_YAML = WORKFLOWS / "ci.yaml"

# GitHub's large-runner labels are the paid ones. A job carrying any of these
# queues forever on an account without the entitlement rather than failing.
LARGE_RUNNER_MARKERS = ("-32-core", "-64-core", "-96-core")


def _ci_jobs() -> dict:
    return yaml.safe_load(CI_YAML.read_text())["jobs"]


def _derive_large_runner_lanes() -> set[str]:
    """Lanes whose sub-workflow requests a large runner, read from the files."""
    lanes = set()
    for name, spec in _ci_jobs().items():
        uses = spec.get("uses", "")
        if not uses.startswith("./"):
            continue
        sub = CI_YAML.parent.parent.parent / uses[2:]
        if not sub.exists():
            continue
        text = sub.read_text()
        if any(marker in text for marker in LARGE_RUNNER_MARKERS):
            lanes.add(name)
    return lanes


def _literal_large_runner_lanes() -> set[str]:
    """The LARGE_RUNNER_LANES tuple as written in the gate's embedded script."""
    text = CI_YAML.read_text()
    match = re.search(r"LARGE_RUNNER_LANES\s*=\s*\(([^)]*)\)", text)
    assert match, "LARGE_RUNNER_LANES literal not found in ci.yaml"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_literal_covers_every_large_runner_lane():
    """Every lane that reaches a large runner must be named in the literal.

    A lane missing here is the silent failure: it would be skipped by the
    entitlement gate and then reported with a tick.
    """
    derived = _derive_large_runner_lanes()
    literal = _literal_large_runner_lanes()
    missing = derived - literal
    assert not missing, (
        f"These lanes use a large runner but are not in LARGE_RUNNER_LANES: "
        f"{sorted(missing)}. They would be skipped and then reported as passing."
    )


def test_literal_names_no_lane_that_does_not_need_one():
    """The reverse drift: a lane that moved back to a standard runner.

    Harmless to CI, but it would print a misleading warning forever, and a
    warning nobody can act on is a warning everyone learns to ignore.
    """
    derived = _derive_large_runner_lanes()
    literal = _literal_large_runner_lanes()
    stale = literal - derived
    assert not stale, (
        f"LARGE_RUNNER_LANES names lanes that no longer use a large runner: "
        f"{sorted(stale)}. Remove them so the warning stays meaningful."
    )


def test_every_large_runner_lane_is_gated_on_the_entitlement_variable():
    """Naming a lane in the literal is only half of it.

    The lane must ALSO be gated on vars.HAS_LARGE_RUNNERS, or it still queues
    forever instead of skipping -- the original defect. `e2e-desktop` is
    exempt: it is disabled outright with a bare `if: false`, so it never
    reaches a runner at all.
    """
    jobs = _ci_jobs()
    ungated = []
    for name in sorted(_derive_large_runner_lanes()):
        condition = str(jobs[name].get("if", ""))
        if condition.strip().lower() in ("false", "${{ false }}"):
            continue  # disabled outright; cannot queue
        if "HAS_LARGE_RUNNERS" not in condition:
            ungated.append(name)
    assert not ungated, (
        f"These lanes reach a large runner but are not gated on "
        f"vars.HAS_LARGE_RUNNERS: {ungated}. They will queue forever."
    )


def test_gate_still_fails_on_a_real_failure():
    """The control: the embedded script must not be blanket-permissive.

    Extract the gate body the way the shell does and drive it with a real
    failure alongside skipped large-runner lanes. If this ever passes with
    exit 0, the gate has stopped discriminating and every lane is green.
    """
    import json
    import subprocess
    import sys
    import tempfile

    text = CI_YAML.read_text()
    jobs = yaml.safe_load(text)["jobs"]
    step = next(s for s in jobs["all-checks-pass"]["steps"] if s.get("id") == "evaluate")
    match = re.search(r'python3 -c "(.*)"\s*$', step["run"], re.S)
    assert match, "could not extract the embedded gate script"
    body = match.group(1).replace('\\"', '"').replace("\\$", "$")

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "gate.py"
        script.write_text(body)
        needs = {
            "tests": {"result": "skipped"},
            "rust-tests": {"result": "skipped"},
            "lint": {"result": "failure"},
        }
        proc = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(needs),
            capture_output=True,
            text=True,
            env={"GITHUB_OUTPUT": str(Path(tmp) / "out.txt"), "PATH": "/usr/bin:/bin"},
        )
    assert proc.returncode == 1, (
        "The gate returned success despite a real failure. It has stopped "
        f"discriminating. stdout:\n{proc.stdout}"
    )
    assert "lint" in proc.stdout
