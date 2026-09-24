"""Every job that requests a paid large runner is gated, and named by the CI gate.

WHY THIS TEST EXISTS
GitHub large runners (``*-32-core``, ``*-96-core``) are a paid feature. On an
account without the entitlement a job that requests one does not fail - it
queues forever. So each such job is gated on ``vars.HAS_LARGE_RUNNERS``, and
``all-checks-pass`` names the jobs that therefore did not run, because a
skipped job would otherwise read as passed.

The unit here is the JOB, or the matrix LEG - never a caller lane in ci.yaml.
A lane can mix paid and free work: ``tests-os`` runs macOS on the free
``macos-latest`` beside a paid Windows leg, and ``tests.yml`` runs a free e2e
job beside the paid suite. The first version of this change gated whole
lanes, and silently switched working macOS coverage off while labelling it
"no large-runner entitlement" - a lie about the reason. These contracts are
written so that shape fails.

The gate's job list is a literal because its job has no checkout; this test
derives the truth from the workflow files and fails when the two drift.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
CI_YAML = WORKFLOWS / "ci.yaml"
LARGE = re.compile(r"-(32|64|96)-core\b")
GATE_VAR = "HAS_LARGE_RUNNERS"
# docker.yml only runs on the upstream repository; its large runners can
# never be requested on a fork, so it needs no entitlement gate.
UPSTREAM_ONLY = "github.repository == 'NousResearch/hermes-agent'"


def _workflows():
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text()) or {}
        yield path.name, doc.get("jobs") or {}


def _include(job: dict):
    """The matrix ``include``, or None. A whole-matrix expression (a string) has none."""
    matrix = (job.get("strategy") or {}).get("matrix")
    return matrix.get("include") if isinstance(matrix, dict) else None


def _legs(job: dict) -> list[dict]:
    """Matrix legs as dicts. A templated ``include`` is parsed out of the expression."""
    include = _include(job)
    if isinstance(include, list):
        return include
    if isinstance(include, str):
        legs = []
        for blob in re.findall(r"'(\[.*?\])'", include):
            for leg in json.loads(blob):
                if leg not in legs:
                    legs.append(leg)
        return legs
    return []


def _large_runner_units() -> dict[str, dict]:
    """``file:job`` or ``file:job[leg name]`` for every unit requesting a large runner."""
    units = {}
    for fname, jobs in _workflows():
        for jname, job in jobs.items():
            runs_on = str(job.get("runs-on", ""))
            if "matrix." in runs_on:
                for leg in _legs(job):
                    if LARGE.search(str(leg.get("runner") or leg.get("os") or "")):
                        units[f"{fname}:{jname}[{leg.get('name')}]"] = {"job": job, "leg": leg}
            elif LARGE.search(runs_on):
                units[f"{fname}:{jname}"] = {"job": job, "leg": None}
    return units


def _gate_literal() -> set[str]:
    text = CI_YAML.read_text()
    match = re.search(r"LARGE_RUNNER_JOBS\s*=\s*\(([^)]*)\)", text)
    assert match, "LARGE_RUNNER_JOBS literal not found in ci.yaml"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def _applies_to_fork(unit: dict) -> bool:
    return UPSTREAM_ONLY not in str(unit["job"].get("if", ""))


def test_every_large_runner_unit_is_gated_on_the_variable():
    """A large-runner job or leg without the gate queues forever - the original defect."""
    ungated = []
    for name, unit in _large_runner_units().items():
        if not _applies_to_fork(unit):
            continue
        if unit["leg"] is None:
            ok = GATE_VAR in str(unit["job"].get("if", ""))
        else:
            # A leg cannot carry `if:`; it is gated by being absent from the
            # matrix unless the variable is set.
            include = str(_include(unit["job"]))
            ok = GATE_VAR in include
        if not ok:
            ungated.append(name)
    assert not ungated, f"large-runner units not gated on vars.{GATE_VAR}: {sorted(ungated)}"


def test_gated_matrix_still_runs_its_free_legs():
    """Gating a leg must not drop the free legs beside it.

    Each gated ``include`` expression has a with-variable list and a
    without-variable list. The without list must be exactly the free legs:
    anything paid left in queues forever, anything free missing is lost
    coverage.
    """
    for fname, jobs in _workflows():
        for jname, job in jobs.items():
            include = _include(job)
            if not (isinstance(include, str) and GATE_VAR in include):
                continue
            blobs = re.findall(r"'(\[.*?\])'", include)
            assert len(blobs) == 2, f"{fname}:{jname}: expected with/without lists, got {len(blobs)}"
            full, reduced = (json.loads(b) for b in blobs)
            free = [leg for leg in full if not LARGE.search(str(leg.get("runner", "")))]
            assert reduced == free, (
                f"{fname}:{jname}: without the entitlement the matrix must be exactly the free "
                f"legs {[l.get('name') for l in free]}, got {[l.get('name') for l in reduced]}"
            )
            assert free, f"{fname}:{jname}: no free legs - gate the job instead"


def test_no_ci_lane_is_gated_on_the_variable():
    """The gate belongs on the job that pays, never on a caller lane.

    A lane-level gate is what dropped macOS: it cannot tell a paid leg from a
    free one beside it.
    """
    jobs = yaml.safe_load(CI_YAML.read_text())["jobs"]
    offenders = [n for n, j in jobs.items() if j.get("uses") and GATE_VAR in str(j.get("if", ""))]
    assert not offenders, f"lanes gated at the caller: {offenders}"


def test_gate_literal_matches_the_workflow_files():
    """Both directions: every fork-reachable large-runner unit named, nothing extra."""
    derived = {n for n, u in _large_runner_units().items() if _applies_to_fork(u)}
    literal = _gate_literal()
    assert not derived - literal, f"missing from LARGE_RUNNER_JOBS: {sorted(derived - literal)}"
    assert not literal - derived, f"stale in LARGE_RUNNER_JOBS: {sorted(literal - derived)}"


def _run_gate(tmp_path: Path, needs: dict, entitled: str) -> subprocess.CompletedProcess:
    jobs = yaml.safe_load(CI_YAML.read_text())["jobs"]
    step = next(s for s in jobs["all-checks-pass"]["steps"] if s.get("id") == "evaluate")
    match = re.search(r'python3 -c "(.*)"\s*$', step["run"], re.S)
    assert match, "could not extract the embedded gate script"
    out = tmp_path / "github_output.txt"
    # The shell expands $GITHUB_OUTPUT inside the double-quoted script before
    # Python sees it. Reproduce that here; otherwise Python opens a file named
    # literally '$GITHUB_OUTPUT' in the working directory.
    body = match.group(1).replace('\\"', '"').replace("$GITHUB_OUTPUT", str(out))
    script = tmp_path / "gate.py"
    script.write_text(body)
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(needs),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"HAS_LARGE_RUNNERS": entitled, "PATH": "/usr/bin:/bin"},
    )


def test_gate_names_skipped_jobs_and_still_fails_on_a_real_failure(tmp_path):
    """The control. Without the entitlement the warning must name the jobs,
    including a leg whose lane reports success; and a real failure must still
    exit 1, or the gate has stopped discriminating."""
    ok = _run_gate(tmp_path, {"tests-os": {"result": "success"}, "lint": {"result": "success"}}, "")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "NOT RUN" in ok.stdout and "Windows-only tests" in ok.stdout

    entitled = _run_gate(tmp_path, {"lint": {"result": "success"}}, "true")
    assert entitled.returncode == 0 and "NOT RUN" not in entitled.stdout

    bad = _run_gate(tmp_path, {"tests-os": {"result": "success"}, "lint": {"result": "failure"}}, "")
    assert bad.returncode == 1, "gate passed despite a real failure:\n" + bad.stdout
    assert "lint" in bad.stdout

    stray = [p.name for p in tmp_path.iterdir() if "GITHUB_OUTPUT" in p.name]
    assert not stray, f"gate wrote to a literal path: {stray}"
