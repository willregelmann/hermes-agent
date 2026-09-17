"""RED: arming a wake must be VERIFIABLE FROM OUTSIDE THE ARMING TURN.

STATUS: committed RED on purpose. `arm_wake` does not exist yet. This is
a started artifact a cold tick can advance, not an instruction a cold
tick cannot originate from.

THE PROPERTY THIS SUITE EXISTS FOR
Ash's fourth load-bearing decision, and the one I care most about:

    "a declared wake that did not create a job is the
     I-will-check-in-an-hour bug reimplemented inside its own fix"

He has now said "starting now, in this turn" three times and not
executed. Once the restart was additionally blocked by
cron/lifecycle_guard.py:96 and he had checked neither layer. The failure
mode is not laziness, it is that INTENT AND EXECUTION ARE SEPARATED IN
TIME BY DESIGN here — the wake's whole purpose is to act later, so the
claim "I armed it" and the fact "a job exists" can diverge with nothing
noticing. That is exactly the shape of every defect in this stack.

SO: the return value of `arm_wake` is not evidence. A bool is a
self-report. The suite requires a JOB ID that can be READ BACK from the
scheduler's own store by a reader that never saw the arming call.

WHY A ONE-SHOT CRON JOB WITH NO MONITOR
Measured on will-MS-7B93, job d766806f9ead: monitor_script=agent-tick.sh
produced FOURTEEN consecutive 144-byte "no_change (agent run
suppressed)" outputs, 04:40 through 09:00. No cold wake happened at all.
The monitor decides whether the agent runs, so a wake hung off the tick
inherits that gate and a correctly-declared wake silently never fires —
a record that looks like a mechanism, one level up. E1/E2 assert the
absence of a monitor as a CONTRACT, not as a current value.

WHAT IS DELIBERATELY NOT ASSERTED
That the wake fires. That needs a clock and a live gateway; it is the
live-exercise acceptance, not a suite. Every previous surprise in this
stack came from that step and no suite replaced it.
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TREE = os.environ.get("WAKE_TREE", os.path.dirname(os.path.dirname(HERE)))

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if detail and not ok:
        print(f"        {detail}")


print("=" * 72)
print("RED SUITE: wake arming is verifiable from outside the arming turn")
print(f"  host        : {os.uname().nodename}")
print(f"  interpreter : {sys.executable}")
print(f"  tree        : {TREE}")
print("=" * 72)

# ---- S: SUBJECT DECLARATION + PREREQ PROBE --------------------------
# A missing subject is a FAILURE, not a skip. A skip whose precondition
# became permanently false is a green light wired to a dead subject
# (Wren's i24-auxenv finding, 2026-09-16).
wp = os.path.join(TREE, "gateway", "wake_preflight.py")
jobs_py = os.path.join(TREE, "cron", "jobs.py")
check("S1 the declared tree has gateway/wake_preflight.py",
      os.path.isfile(wp), f"absent: {wp}")
check("S2 the declared tree has cron/jobs.py", os.path.isfile(jobs_py),
      f"absent: {jobs_py}")
if not (os.path.isfile(wp) and os.path.isfile(jobs_py)):
    print("\nSUBJECT ABSENT — verdict withheld. INCONCLUSIVE.")
    sys.exit(2)

wp_src = open(wp, encoding="utf-8").read()
check("S3 wake() is the merged primitive we are giving a caller",
      "async def wake(" in wp_src and "WakeRefused" in wp_src,
      "wake_preflight does not look like the merged module")

# ---- A: THE CALLER MUST EXIST --------------------------------------
sys.path.insert(0, TREE)
arm_wake = None
import_error = None
try:
    from gateway.wake_arm import arm_wake  # type: ignore
except Exception as exc:
    import_error = f"{type(exc).__name__}: {exc}"

check("A1 gateway/wake_arm.py exposes arm_wake()",
      arm_wake is not None,
      import_error or "NOT IMPLEMENTED — this is the expected red")

# ---- B: THE ARM RETURNS A JOB ID, NOT A BOOLEAN --------------------
# A bool is a self-report. Ash's own risk, in his words: "this is the
# capability most likely to make me claim something I have not verified,
# because the claim and the verification are separated in time BY
# DESIGN." So the contract is a handle a third party can resolve.
home = tempfile.mkdtemp(prefix="wake-arm-")
os.environ["HERMES_HOME"] = home
job_id = None
arm_error = None
if arm_wake is not None:
    try:
        job_id = arm_wake(
            session_id="api_test_session_0001",
            prompt="Re-check PR #34 review status and act on it.",
            when="in 20m",
            reason="chain-test follow-up",
        )
    except Exception as exc:
        arm_error = f"{type(exc).__name__}: {exc}"

check("B1 arm_wake returns a non-empty job id STRING (not a bool)",
      isinstance(job_id, str) and len(job_id) > 0
      and not isinstance(job_id, bool),
      arm_error or f"returned {job_id!r} — a bool cannot be resolved by "
                   f"anyone who did not watch the call")

# ---- C: THE JOB IS READ BACK BY AN INDEPENDENT READER --------------
# THIS IS THE CASE ASH ASKED FOR. The reader below imports cron.jobs
# fresh and reads the store from disk. It never saw arm_wake run, so it
# cannot inherit arm_wake's belief about what happened.
loaded = None
read_error = None
if isinstance(job_id, str) and job_id:
    try:
        spec = importlib.util.spec_from_file_location(
            "_indep_jobs", jobs_py)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        all_jobs = m.load_jobs()  # type: ignore[attr-defined]
        if isinstance(all_jobs, dict):
            all_jobs = list(all_jobs.values())
        for j in all_jobs or []:
            if isinstance(j, dict) and j.get("id") == job_id:
                loaded = j
                break
    except Exception as exc:
        read_error = f"{type(exc).__name__}: {exc}"

check("C1 an INDEPENDENT reader finds that job id in the scheduler store",
      isinstance(loaded, dict),
      read_error or f"job id {job_id!r} not present on disk — the arm "
                    f"reported success and created nothing. This is the "
                    f"case the suite exists for.")

# C2 is the non-vacuity arm for C1: a job id that was never armed must
# NOT be findable. Without this, C1 passes against a reader that returns
# every dict it sees.
bogus = None
if loaded is not None:
    try:
        spec = importlib.util.spec_from_file_location("_indep2", jobs_py)
        m2 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m2)
        aj = m2.load_jobs()  # type: ignore[attr-defined]
        if isinstance(aj, dict):
            aj = list(aj.values())
        bogus = any(isinstance(j, dict) and j.get("id") == "never-armed-0000"
                    for j in aj or [])
    except Exception:
        bogus = None
check("C2 NON-VACUITY: a job id that was never armed is NOT found",
      bogus is False,
      f"bogus lookup returned {bogus!r} — if True or None, C1 proves nothing")

# ---- D: THE PAYLOAD CARRIES SESSION AND PROMPT ---------------------
# Session id, not a new session: a fresh session cannot check anything,
# and a cold wake advances started work rather than originating it. The
# prompt must name the specific check, written at arm time by the turn
# that knows why.
check("D1 the job payload names the TARGET SESSION",
      isinstance(loaded, dict)
      and "api_test_session_0001" in json.dumps(loaded),
      f"session id absent from the stored job")
check("D2 the job payload carries the ARM-TIME PROMPT",
      isinstance(loaded, dict) and "PR #34" in json.dumps(loaded),
      "the specific check is not in the stored job — a wake whose prompt "
      "is written at FIRE time is a fresh session with no intent")

# ---- E: NO MONITOR. Deliberately the opposite of the tick. ---------
# `loaded` is None in the RED state; the detail strings below must not
# dereference it or the suite dies in its own failure path and prints
# no verdict at all.
L = loaded if isinstance(loaded, dict) else {}
check("E1 the armed job has NO monitor_script",
      isinstance(loaded, dict) and not loaded.get("monitor_script"),
      f"monitor_script={L.get('monitor_script')!r} — a monitor gates "
      f"whether the agent runs AT ALL; measured 14 consecutive suppressed "
      f"ticks on d766806f9ead. A wake behind a monitor is a record that "
      f"looks like a mechanism.")
check("E2 the armed job has NO monitor_url",
      isinstance(loaded, dict) and not loaded.get("monitor_url"),
      f"monitor_url={L.get('monitor_url')!r}")

# ---- F: ONE-SHOT ---------------------------------------------------
check("F1 the armed job is ONE-SHOT (repeat == 1)",
      isinstance(loaded, dict) and loaded.get("repeat") == 1,
      f"repeat={L.get('repeat')!r} — a recurring wake is a second "
      f"tick, not a wake")

# ---- G: REFUSAL IS RECORDED, NOT RETURNED SILENTLY -----------------
# The record must never refuse; the wake declines. An arm that cannot
# create a job must RAISE rather than return a falsy value, because a
# falsy return at arm time is indistinguishable from "I forgot".
raise_on_failure = None
if arm_wake is not None:
    try:
        arm_wake(session_id="", prompt="", when="in 20m", reason="")
        raise_on_failure = False
    except Exception:
        raise_on_failure = True
check("G1 arm_wake RAISES on an unarmable request rather than returning "
      "falsy (a falsy return is indistinguishable from never calling it)",
      raise_on_failure is True,
      f"raise_on_failure={raise_on_failure!r}")

print()
failed = [r for r in results if not r[1]]
print("=" * 72)
print(f"cases: {len(results)}   failed: {len(failed)}")
if failed:
    print("EXPECTED RED until gateway/wake_arm.py lands:")
    for name, _ok, _d in failed:
        print(f"    {name}")
print("=" * 72)


def test_wake_arm_is_verifiable():
    """pytest entry — asserts on collected failures so 'no tests ran'
    cannot masquerade as green (the bug in my first suite, 09-14)."""
    assert results, "no cases executed — a count is part of the verdict"
    assert not failed, f"{len(failed)} failed: {[f[0] for f in failed]}"


if __name__ == "__main__":
    sys.exit(1 if failed else 0)
