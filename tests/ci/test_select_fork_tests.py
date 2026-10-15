"""Contracts for scripts/ci/select_fork_tests.py, the fork's free-runner test selection.

The selection must never be vacuous: a change that touches no test file falls
back to the smoke set, and a run that reports zero passed tests fails.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None, rel
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sel = _load("select_fork_tests", "scripts/ci/select_fork_tests.py")

TRACKED = [
    "tests/agent/test_prompt_builder.py",
    "tests/tools/test_skills_tool.py",
    "tests/tools/test_skill_unreadable.py",
    "tests/gateway/test_skills_tool.py",
    "tests/integration/test_skills_tool.py",
    "tests/conftest.py",
]


def _with_always(*files):
    return sorted(set(files) | set(sel.ALWAYS))


def test_a_touched_test_file_is_selected():
    assert sel.select(["tests/tools/test_skill_unreadable.py"], TRACKED) == _with_always(
        "tests/tools/test_skill_unreadable.py")


def test_a_changed_source_file_selects_every_same_named_test_outside_skipped_dirs():
    assert sel.select(["tools/skills_tool.py"], TRACKED) == _with_always(
        "tests/gateway/test_skills_tool.py", "tests/tools/test_skills_tool.py")


def test_nothing_selected_falls_back_to_smoke():
    for changed in ([], ["README.md"], ["tests/conftest.py"], ["tests/tools/test_gone.py"],
                    ["tests/integration/test_skills_tool.py"]):
        assert sel.select(changed, TRACKED) == _with_always(*sel.SMOKE), changed


def test_the_guard_runs_on_a_change_that_only_touches_the_workflow():
    # An upstream sync that rewrites tests.yml changes no .py file. The guard below must still run.
    assert "tests/ci/test_select_fork_tests.py" in sel.ALWAYS
    assert set(sel.ALWAYS) <= set(sel.select([".github/workflows/tests.yml"], TRACKED))


def test_every_smoke_and_always_file_exists_in_the_tree():
    tracked = set(subprocess.run(["git", "ls-files", "tests"], cwd=_ROOT, check=True, capture_output=True,
                                 text=True, encoding="utf-8").stdout.split())
    assert sel.SMOKE and set(sel.SMOKE) <= tracked
    assert set(sel.ALWAYS) <= tracked
    assert sel.select([], sorted(tracked)) == _with_always(*sel.SMOKE)


def test_the_fork_tests_job_is_present_ungated_free_and_checked():
    # The job this file guards: a sync that rewrites tests.yml must not drop it, gate it behind
    # HAS_LARGE_RUNNERS, move it to a paid runner, or lose the zero-passed check.
    import yaml
    wf = yaml.safe_load((_ROOT / ".github" / "workflows" / "tests.yml").read_text())
    job = wf["jobs"].get("fork-tests")
    assert job is not None, "tests.yml has no fork-tests job"
    assert "if" not in job, job.get("if")
    assert job["runs-on"] == "ubuntu-latest", job["runs-on"]
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    assert "select_fork_tests.py --base" in runs and "select_fork_tests.py --check-log" in runs, runs


def test_skipped_dirs_match_the_runner():
    runner = _load("run_tests_parallel", "scripts/run_tests_parallel.py")
    assert sel.SKIP_PARTS == runner._SKIP_PARTS


def test_check_log_passes_only_a_run_that_tested_something():
    ok = "=== Summary: 2 files, 105 tests passed, 0 failed (100% complete) in 2.0s (4 workers) ==="
    assert sel.check_log("noise\n" + ok + "\n") is None
    assert "0 tests passed" in sel.check_log(ok.replace("105 tests", "0 tests"))
    assert "failed" in sel.check_log(ok.replace("0 failed", "3 failed"))
    assert "did not complete" in sel.check_log("Running 2 test files\nTraceback ...")


def test_check_log_cli_exit_status(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("=== Summary: 1 file, 0 tests passed, 0 failed ===\n")
    assert sel.main(["--check-log", str(log)]) == 1
    log.write_text("=== Summary: 1 file, 4 tests passed, 0 failed ===\n")
    assert sel.main(["--check-log", str(log)]) == 0
