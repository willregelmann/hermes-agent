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
# WREN'S CORRECTION to Ash's original F1, which asserted `repeat == 1`.
# That froze the ARGUMENT SHAPE, not the stored contract: create_job
# normalises repeat=1 into {"times": 1, "completed": 0}, and the shape
# production actually reads is `_repeat.get("times") == 1` at
# cron/scheduler.py:6306 (_finite_oneshot). So the original case failed
# against a correct subject and would have passed for a job that stored a
# bare 1 the scheduler does not recognise as finite.
#
# This is the third instance this week of an assertion about the present
# masquerading as an assertion about the contract (Ash's H5 on #34, Wren's
# i24-auxenv arm, and now this). Assert what the scheduler READS.
_rp = L.get("repeat")
_times = _rp.get("times") if isinstance(_rp, dict) else _rp
check("F1 the armed job is ONE-SHOT as the SCHEDULER reads it "
      "(repeat.times == 1, per _finite_oneshot)",
      isinstance(loaded, dict) and _times == 1,
      f"repeat={_rp!r} — a recurring wake is a second tick, not a wake")
check("F2 NON-VACUITY: the schedule is a 'once' kind, so _finite_oneshot "
      "can be satisfied at all",
      isinstance(loaded, dict)
      and isinstance(loaded.get("schedule"), dict)
      and loaded["schedule"].get("kind") == "once",
      f"schedule={L.get('schedule')!r} — _finite_oneshot requires BOTH "
      f"kind=='once' AND repeat.times==1; asserting only the repeat half "
      f"passes for a recurring job that happens to count to one")

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

# ---- D3: THE SESSION MUST BE IN A FIELD, NOT ONLY IN THE LABEL -----
# MUTANT M1 (round 10) deleted the line that attaches the session id to the
# job payload and passed all 14 cases. D1 searches json.dumps(job), and the
# session id was present there only via `name` -- which M2 also mutates, so
# the two arms shared a single binding. A firing side cannot key on a label.
# This arm reads the DEDICATED FIELD from the store, via the independent
# reader, so "the label happens to contain it" cannot satisfy it.
check("D3 the STORED job carries the session in a dedicated field "
      "(wake_session_id), not merely inside the human-readable name",
      isinstance(loaded, dict)
      and loaded.get("wake_session_id") == "api_test_session_0001",
      f"wake_session_id={L.get('wake_session_id')!r} — create_job has "
      f"already saved the store before it returns, so mutating the returned "
      f"dict persists nothing")

# ---- H: ONE ARM PER VALIDATED ARGUMENT -----------------------------
# G1 passes ALL THREE arguments empty at once, so it binds whichever check
# happens to fire first. Round 10 deleted each of the three guards
# individually and all three mutants survived. An invariant quantified over
# a SET OF SITES needs one case per site (lesson 73).
def _arm_refuses(**kw):
    """True iff arm_wake raises AND leaves no job behind."""
    if arm_wake is None:
        return None
    args = dict(session_id="api_h_session", prompt="H probe", when="in 20m",
                reason="h")
    args.update(kw)
    before = _job_ids()
    try:
        arm_wake(**args)
        raised = False
    except Exception:
        raised = True
    return raised and _job_ids() == before


def _job_ids():
    try:
        spec = importlib.util.spec_from_file_location("_indep_h", jobs_py)
        mh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mh)
        aj = mh.load_jobs()
        if isinstance(aj, dict):
            aj = list(aj.values())
        return {j.get("id") for j in aj or [] if isinstance(j, dict)}
    except Exception:
        return None


check("H1 an EMPTY session_id is refused with prompt and when both valid",
      _arm_refuses(session_id="  ") is True,
      "a wake armed with no session resumes nothing, and `wake:` as a name "
      "hides it")
# H2/H3 MEASURED CORRECTION (round 10): create_job ALREADY refuses a blank
# prompt (ValueError "nothing to run") and a bad schedule, so mutants that
# deleted wake_arm's own prompt/when guards SURVIVED -- the turn still saw an
# exception, from one layer down. Lesson 54's first triage question is "does
# the underlying tool already refuse?", and here it does. What the local
# guards add is therefore NOT the refusal but the fact that the store is
# NEVER CALLED: a missing argument is caught at the arming site, named, and
# the scheduler is never asked. That is the distinction these arms bind, by
# counting calls through a stub -- an exception type cannot tell the two
# layers apart because wake_arm wraps the lower one in the same WakeArmError.
def _refuses_without_calling_store(**kw):
    """(raised_WakeArmError, create_job_call_count, message)."""
    if arm_wake is None:
        return (None, None, "")
    import cron.jobs as _cj
    import gateway.wake_arm as _wa
    args = dict(session_id="api_h_session", prompt="H probe", when="in 20m",
                reason="h")
    args.update(kw)
    calls = []
    real = _cj.create_job

    def _spy(*a, **k):
        calls.append(k)
        return real(*a, **k)

    _cj.create_job = _spy
    try:
        arm_wake(**args)
        return (False, len(calls), "no exception")
    except _wa.WakeArmError as exc:
        return (True, len(calls), str(exc))
    except Exception as exc:
        return (False, len(calls), f"{type(exc).__name__}: {exc}")
    finally:
        _cj.create_job = real


_h2 = _refuses_without_calling_store(prompt="   ")
check("H2 a BLANK prompt is refused AT THE ARMING SITE — WakeArmError, the "
      "store is never called, and the message names the prompt",
      _h2[0] is True and _h2[1] == 0 and "prompt" in _h2[2].lower(),
      f"raised={_h2[0]!r} create_job_calls={_h2[1]!r} msg={_h2[2]!r} — if "
      f"calls==1 the local guard is gone and the refusal came from "
      f"create_job one layer down")
_h3 = _refuses_without_calling_store(when=None)
check("H3 a MISSING when is refused AT THE ARMING SITE — WakeArmError, the "
      "store is never called",
      _h3[0] is True and _h3[1] == 0 and "when" in _h3[2].lower(),
      f"raised={_h3[0]!r} create_job_calls={_h3[1]!r} msg={_h3[2]!r} — "
      f"create_job raises a bare AttributeError on when=None, so 'something "
      f"threw' does not distinguish the guard")

# ---- H4: THE NAME IS THE HUMAN HANDLE ------------------------------
# M2 stripped the session id out of the job NAME and survived the patched
# suite, because D1 (json.dumps contains the id) is now satisfied by the
# wake_session_id field D3 added. Two arms had collapsed onto one binding.
# The name is how a person finds this job in `hermes cron list`; assert it
# separately from the machine-readable field.
check("H4 the STORED job NAME carries the session id (the handle a human "
      "greps for), independently of the wake_session_id field",
      isinstance(loaded, dict)
      and "api_test_session_0001" in str(loaded.get("name") or ""),
      f"name={L.get('name')!r}")

# ---- I: A create_job THAT CREATED NOTHING MUST RAISE ---------------
# M10 deleted the non-dict guard and survived: the happy path never returns
# a non-dict, so the branch had no arm at all. Stub the store's writer.
# M10 deleted the non-dict guard and survived: with the guard gone,
# job.get() on None raises AttributeError, so "something was raised" is
# TRUE EITHER WAY. The contract is the TYPE — WakeArmError, the module's own
# signal that nothing was armed — not merely that the turn was interrupted.
_i1 = None
_i1_detail = ""
if arm_wake is not None:
    import cron.jobs as _cj
    import gateway.wake_arm as _wa
    _real_create = _cj.create_job
    try:
        _cj.create_job = lambda *a, **k: None
        try:
            arm_wake(session_id="api_i_session", prompt="I probe",
                     when="in 20m", reason="i")
            _i1, _i1_detail = False, "no exception"
        except _wa.WakeArmError as exc:
            _i1, _i1_detail = True, str(exc)
        except Exception as exc:
            _i1 = False
            _i1_detail = f"{type(exc).__name__}: {exc}"
    finally:
        _cj.create_job = _real_create
check("I1 a create_job that returns a NON-RECORD raises WakeArmError "
      "specifically (not an incidental AttributeError from the next line)",
      _i1 is True,
      f"_i1={_i1!r} detail={_i1_detail!r} — an AttributeError also stops the "
      f"turn, so only the type distinguishes 'nothing was armed' from a crash")

# ---- I2: THE RE-READ IS THE VERIFICATION, NOT update_job's RETURN ---
# M1c deleted the re-read-and-compare and survived, because update_job in
# fact works. The branch exists for the case where it silently does not —
# exactly the failure class this module was built for. Stub update_job into
# a no-op that reports success and assert arm_wake refuses anyway.
_i2 = None
_i2_detail = ""
if arm_wake is not None:
    import cron.jobs as _cj
    import gateway.wake_arm as _wa
    _real_update = _cj.update_job
    try:
        _cj.update_job = lambda job_id, updates: {"id": job_id}
        try:
            arm_wake(session_id="api_i2_session", prompt="I2 probe",
                     when="in 20m", reason="i2")
            _i2, _i2_detail = False, "returned a job id"
        except _wa.WakeArmError as exc:
            _i2, _i2_detail = True, str(exc)
        except Exception as exc:
            _i2 = False
            _i2_detail = f"{type(exc).__name__}: {exc}"
    finally:
        _cj.update_job = _real_update
check("I2 a persist that REPORTS SUCCESS but writes nothing is caught by "
      "re-reading the store (the arm cannot trust its own writer)",
      _i2 is True,
      f"_i2={_i2!r} detail={_i2_detail!r} — without the re-read, arm_wake "
      f"returns a job id whose stored record has no session to resume, and "
      f"that is a self-report wearing a handle")

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
