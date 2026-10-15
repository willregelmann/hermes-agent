"""Pick the Python test files the fork's free-runner CI job runs.

The full suite (tests.yml) needs a paid 96-core runner, so on this fork it
never runs. This job runs on ubuntu-latest instead, over a selection:

* every test file the change touches,
* every test file named for a changed source file (``foo.py`` ->
  ``tests/**/test_foo.py``),
* and, when that selects nothing, a fixed smoke set, so the job is never
  vacuous;
* plus, always, this job's own guard (tests/ci/test_select_fork_tests.py),
  so a change that only touches the workflow still checks the job survived.

``--check-log`` reads the runner's output afterwards and fails unless it
reports at least one passed test: a selection that ran nothing is a failure,
not a pass.

Usage:
    select_fork_tests.py --base <sha>        print a colon-joined file list
    select_fork_tests.py --check-log <file>  exit 1 unless tests ran and passed
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import PurePosixPath

# Fast, broad, and stable. Each must exist (tests/ci/test_select_fork_tests.py).
SMOKE = (
    "tests/agent/test_prompt_builder.py",
    "tests/agent/test_skill_utils.py",
    "tests/tools/test_registry.py",
    "tests/tools/test_skills_tool.py",
    "tests/ci/test_classify_changes.py",
)

# Run on every selection. It guards this job itself (ungated, on a free
# runner, failing on zero passed). A sync that rewrites tests.yml changes
# no .py file, so name-matching alone would never select the guard.
ALWAYS = ("tests/ci/test_select_fork_tests.py",)

# Directories run_tests_parallel.py skips by default; they need services this
# runner doesn't have. Must equal run_tests_parallel._SKIP_PARTS (tested).
SKIP_PARTS = {"integration", "e2e", "docker"}

_SUMMARY = re.compile(r"=== Summary: (\d+) files?, (\d+) tests? passed, (\d+) failed")


def _is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    return (p.parts[:1] == ("tests",) and p.name.startswith("test_") and p.suffix == ".py"
            and not SKIP_PARTS.intersection(p.parts))


def select(changed: list[str], tracked: list[str]) -> list[str]:
    """Test files to run for ``changed``, drawn from ``tracked`` (files at HEAD)."""
    tests = [t for t in tracked if _is_test_file(t)]
    by_name: dict[str, list[str]] = {}
    for t in tests:
        by_name.setdefault(PurePosixPath(t).name, []).append(t)
    present = set(tests)
    picked: set[str] = set()
    for path in changed:
        if path in present:
            picked.add(path)
        elif path.endswith(".py") and not path.startswith("tests/"):
            picked.update(by_name.get(f"test_{PurePosixPath(path).stem}.py", ()))
    return sorted((picked or set(SMOKE)) | set(ALWAYS))


def check_log(text: str) -> str | None:
    """None when the runner reported a pass of at least one test, else why not."""
    m = _SUMMARY.search(text)
    if not m:
        return "no runner summary line: the test run did not complete"
    files, passed, failed = (int(g) for g in m.groups())
    if failed:
        return f"{failed} test(s) failed"
    if passed == 0:
        return f"{files} file(s) selected but 0 tests passed: nothing was tested"
    return None


def _git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--base", help="commit to diff HEAD against; empty or all-zero means smoke")
    mode.add_argument("--check-log", metavar="FILE")
    args = ap.parse_args(argv)
    if args.check_log is not None:
        with open(args.check_log, encoding="utf-8", errors="replace") as f:
            why = check_log(f.read())
        if why:
            print(f"::error::fork tests: {why}")
            return 1
        return 0
    base = (args.base or "").strip()
    changed = [] if not base or set(base) == {"0"} else _git("diff", "--name-only", f"{base}...HEAD")
    files = select(changed, _git("ls-files", "tests"))
    print(f"{len(changed)} changed file(s) -> {len(files)} test file(s)", file=sys.stderr)
    for f in files:
        print(f"  {f}", file=sys.stderr)
    print(":".join(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
