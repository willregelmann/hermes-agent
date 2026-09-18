"""D1 arms, added by Wren after Ash's CHANGES_REQUESTED on PR #41.

THE DEFECT: gateway/wake_arm.py's docstring said "Recurring forms are
refused — a repeating wake is a second tick." Nothing refused them.
Measured on e72c1be9ce:

    arm_wake(when="every 10m")
      -> ARMED, schedule={'kind': 'interval', 'minutes': 10}
      -> repeat={'times': 1, 'completed': 0}

So the stored schedule was an INTERVAL and the only thing making it
terminal was a counter. Behaviour was one-shot-ish; the contract was
violated. Ash's name for it: C1-decoration from #32 in a new place — a
property a reader takes as checked.

WHY THE ASSERTIONS READ THE STORED JOB, NOT THE INPUT STRING:
parse_schedule decides what "every 10m" means. Validating the string
would assert MY READING of the parser rather than the parser's output,
which is the same mistake as asserting repeat==1 when the store holds
{"times": 1} — Ash's original F1, which failed against a correct subject.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile

TREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, TREE)

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        {detail}")
        FAILED.append(name)


def _load_jobs_independently():
    """Read the store with a module that never saw arm_wake run."""
    spec = importlib.util.spec_from_file_location(
        "_d1_jobs", os.path.join(TREE, "cron", "jobs.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    jobs = m.load_jobs()
    return list(jobs.values()) if isinstance(jobs, dict) else (jobs or [])


print("=" * 70)

os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="d1-once-")
from gateway.wake_arm import WakeArmError, arm_wake  # noqa: E402

# ---- D1: a recurring form is REFUSED -------------------------------
recurring_error = None
recurring_id = None
try:
    recurring_id = arm_wake(session_id="api_d1", prompt="p", when="every 10m")
except WakeArmError as exc:
    recurring_error = str(exc)

check("D1 a recurring form ('every 10m') is REFUSED",
      recurring_error is not None,
      f"armed as {recurring_id!r} — the docstring promises refusal and the "
      f"stored schedule would be kind='interval', terminal only by a counter")

check("D1b the refusal NAMES the parsed kind, so the operator learns why",
      recurring_error is not None and "interval" in recurring_error,
      f"reason={recurring_error!r} — a refusal that does not say what the "
      f"input parsed to cannot be acted on")

# ---- D1c: NOTHING IS LEFT ON DISK after a refusal -------------------
# The refusal happens AFTER create_job, so the rollback is the load-bearing
# half. A refused arm that leaves a recurring job armed is worse than no
# check at all: the turn reports failure while the job fires anyway.
leftover = [j for j in _load_jobs_independently()
            if isinstance(j, dict)
            and str(j.get("name", "")).startswith("wake:api_d1")]
check("D1c a REFUSED arm leaves NO job on disk (rollback, read independently)",
      leftover == [],
      f"{len(leftover)} job(s) still armed: "
      f"{[j.get('schedule') for j in leftover]} — the arm raised and the job "
      f"survived, so a recurring wake is live while the turn believes it is not")

# ---- D2: NON-VACUITY. The one-shot form still works -----------------
# Without this, D1 passes for a subject that refuses EVERYTHING, which
# would be the H5 failure mode: a fix that reads as "stop checking" in
# reverse. Same discriminating-pair discipline as G3-vs-H1 on #34.
once_id = None
once_err = None
try:
    once_id = arm_wake(session_id="api_d2", prompt="p", when="in 20m")
except Exception as exc:
    once_err = f"{type(exc).__name__}: {exc}"

check("D2 NON-VACUITY: a one-shot form ('in 20m') STILL ARMS",
      isinstance(once_id, str) and once_id,
      once_err or f"returned {once_id!r} — if this fails the refusal is not a "
                  f"check, it is an outage")

stored = [j for j in _load_jobs_independently()
          if isinstance(j, dict) and j.get("id") == once_id]
check("D2b and its STORED schedule kind is 'once' (what the scheduler reads)",
      stored and isinstance(stored[0].get("schedule"), dict)
      and stored[0]["schedule"].get("kind") == "once",
      f"schedule={stored[0].get('schedule') if stored else None!r}")

print()
print(f"cases: 5   failed: {len(FAILED)}")
if FAILED:
    for f in FAILED:
        print(f"    {f}")
print("=" * 70)
