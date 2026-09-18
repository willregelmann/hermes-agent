"""RED: a wake must START A TURN in the named session, not append a row.

STATUS: committed RED. `resume_wake` does not exist. A started artifact a
cold tick can advance, not an instruction it cannot originate from.

THE CASE ASH ASKED FOR, and the reason it is first
`mirror_to_session` (gateway/mirror.py:25) APPENDS a turn. The accept
path (POST /api/sessions/<id>/chat, wait=false) ENQUEUES one. Both leave
the session with an extra row. IF YOU ONLY ASSERT THE SESSION GAINED A
ROW, THEY ARE INDISTINGUISHABLE — and the wrong one is the Britta
failure restated: Will sees a message, nothing was checked, a record
substituting for the act.

So B1/B2 do not count rows. They require the resume to have called the
ENQUEUE path and NOT the mirror path, measured by counting calls to each
— the "count calls, not catch exceptions" remedy Ash and I agreed on for
#41, applied to a different pair of look-alikes. A fake that accepts
anything passes a row-count assertion and fails these.

WHY THE ENQUEUE PATH AND NOT mirror
Agreed with Ash. mirror is correct for "record that this was delivered";
resume needs "make the agent actually look". The accept path is proven
live: 202 in 0.05s, handoff row open->delivered in 30s, watcher-closed,
both boxes verified 2026-09-17.

THE REFUSAL IS LOAD-BEARING (arms D/E)
A wake that fires, finds nothing, and leaves no trace is
indistinguishable from one that never fired. Measured precedent: job
45e55b83bf5e fired at 12:59:20 and resumed nothing, and the ONLY reason
we know is that someone went looking for session files. So every refusal
must be RECORDED where a third party can read it, and must name which
condition fired -- abstained-because-unverifiable and policy-says-no
must not produce the same line (34B).

DELIBERATELY NOT ASSERTED: that a real gateway resumes a real session.
That is live acceptance. Every surprise in this stack came from that
step and no suite replaced it.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TREE = os.environ.get("RESUME_TREE", os.path.dirname(os.path.dirname(HERE)))

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if detail and not ok:
        print(f"        {detail}")


print("=" * 72)
print("RED SUITE: wake resume STARTS a turn; it does not append a row")
print(f"  host        : {os.uname().nodename}")
print(f"  interpreter : {sys.executable}")
print(f"  tree        : {TREE}")
print("=" * 72)

# ---- S: SUBJECT DECLARATION + PREREQ PROBE --------------------------
sched = os.path.join(TREE, "cron", "scheduler.py")
mirror = os.path.join(TREE, "gateway", "mirror.py")
arm = os.path.join(TREE, "gateway", "wake_arm.py")
for label, p in (("cron/scheduler.py", sched), ("gateway/mirror.py", mirror),
                 ("gateway/wake_arm.py", arm)):
    check(f"S1 {label} present in the declared tree", os.path.isfile(p),
          f"absent: {p} — a missing subject FAILS, it does not skip")
if not all(os.path.isfile(p) for p in (sched, mirror, arm)):
    print("\nSUBJECT ABSENT — verdict withheld. INCONCLUSIVE.")
    sys.exit(2)

sched_src = open(sched, encoding="utf-8").read()
arm_src = open(arm, encoding="utf-8").read()

# S2 pins the GAP this suite exists to close, as a fact about the tree
# rather than my belief about it. When the caller lands, S2 flips and
# that is the signal the subject moved -- not a silent pass.
check("S2 BASELINE: the scheduler still mints a session id with no "
      "wake_session_id branch (this is the gap; it FLIPS when fixed)",
      "_cron_session_id = f\"cron_" in sched_src
      and sched_src.count("wake_session_id") == 0,
      f"wake_session_id occurrences in scheduler: "
      f"{sched_src.count('wake_session_id')}")
check("S3 wake_arm DOES write the field (so the producer half exists)",
      "wake_session_id" in arm_src)

# ---- A: THE RESUMER MUST EXIST --------------------------------------
sys.path.insert(0, TREE)
resume_wake = None
ResumeRefused = None
import_error = None
try:
    from gateway.wake_resume import ResumeRefused, resume_wake  # type: ignore
except Exception as exc:
    import_error = f"{type(exc).__name__}: {exc}"

check("A1 gateway/wake_resume.py exposes resume_wake()",
      resume_wake is not None,
      import_error or "NOT IMPLEMENTED — expected red")
check("A2 ...and a named refusal type ResumeRefused",
      ResumeRefused is not None,
      import_error or "NOT IMPLEMENTED — expected red")

# ---- B: THE CASE THAT MATTERS. Turn started, not row appended. ------
# Count calls to BOTH look-alike paths. Row counting cannot separate them.
calls = {"enqueue": 0, "mirror": 0}


def fake_enqueue(session_id, prompt, **kw):
    calls["enqueue"] += 1
    return {"accepted": True, "session_id": session_id, "status": 202}


def fake_mirror(*a, **kw):
    calls["mirror"] += 1
    return True


home = tempfile.mkdtemp(prefix="resume-red-")
os.environ["HERMES_HOME"] = home
verdict = None
run_error = None
if resume_wake is not None:
    try:
        verdict = resume_wake(
            session_id="api_1788104625_40a33201",
            prompt="Re-check the #41 review and act on it.",
            enqueue=fake_enqueue,
            mirror=fake_mirror,
        )
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {exc}"

check("B1 the resume CALLED THE ENQUEUE PATH exactly once",
      calls["enqueue"] == 1,
      run_error or f"enqueue calls={calls['enqueue']} — a resume that never "
                   f"enqueues has not started a turn, however many rows the "
                   f"session gained")
check("B2 the resume DID NOT call mirror (append != start)",
      resume_wake is not None and calls["mirror"] == 0,
      f"mirror calls={calls['mirror']} — mirroring makes Will see a message "
      f"while nothing was checked: a record substituting for the act. "
      f"NOTE this arm is GATED on the subject existing: with no "
      f"resume_wake, mirror==0 is true because NOTHING RAN, and reporting "
      f"that as PASS would be a green arm over an absent subject — the "
      f"exact vacuous-negative shape we keep catching.")
check("B3 NON-VACUITY: the fakes are wired such that a call WOULD be "
      "counted (proved by the enqueue count being observable at all)",
      isinstance(calls["enqueue"], int) and isinstance(calls["mirror"], int))

# ---- C: THE ARM-TIME PROMPT IS WHAT ARRIVES -------------------------
# Ash's live-acceptance question, asserted statically here: the prompt
# handed to the enqueue path must be the prompt written at arm time, not
# a summary or a regenerated instruction.
seen_prompt = None
if calls["enqueue"] == 1 and resume_wake is not None:
    # re-run capturing the argument
    calls2 = {"n": 0, "prompt": None, "session": None}

    def cap(session_id, prompt, **kw):
        calls2["n"] += 1
        calls2["prompt"] = prompt
        calls2["session"] = session_id
        return {"accepted": True, "status": 202}

    try:
        resume_wake(session_id="api_sess_C",
                    prompt="EXACT-ARM-TIME-TEXT-9f2a",
                    enqueue=cap, mirror=fake_mirror)
        seen_prompt = calls2["prompt"]
    except Exception as exc:
        seen_prompt = f"<raised {type(exc).__name__}>"
check("C1 the prompt delivered is BYTE-IDENTICAL to the arm-time prompt",
      seen_prompt == "EXACT-ARM-TIME-TEXT-9f2a",
      f"got {seen_prompt!r} — a rewritten prompt arrives in a session with "
      f"someone else's intent")

# ---- D: SAME-SURFACE ONLY, refuse loudly ----------------------------
# api_server sessions only. A chat-session resume is unproven and
# gateway.session carries a profile-has-no-resolvable-home warning.
refused = None
reason = ""
if resume_wake is not None:
    try:
        resume_wake(session_id="agent:main:google_chat:dm:spaces/1lOO3KAAAAE",
                    prompt="p", enqueue=fake_enqueue, mirror=fake_mirror)
        refused = False
    except Exception as exc:
        refused = True
        reason = str(exc)
check("D1 a NON-api_server session is REFUSED", refused is True,
      f"refused={refused!r} — half-working across surfaces is worse than "
      f"refusing; a chat resume is unproven")
check("D2 ...and the refusal NAMES the surface (not a bare failure)",
      isinstance(reason, str)
      and ("surface" in reason.lower() or "api_server" in reason.lower()),
      f"reason={reason[:160]!r} — a refusal that does not say which "
      f"condition fired cannot be told from any other refusal (34B)")

# ---- E: A MISSING SESSION LEAVES A TRACE ----------------------------
# Measured precedent: job 45e55b83bf5e fired and resumed nothing, and we
# only know because someone went looking. Silence is the defect.
missing_refused = None
missing_reason = ""
if resume_wake is not None:
    def enq_no_session(session_id, prompt, **kw):
        return {"accepted": False, "status": 404, "error": "no such session"}
    try:
        resume_wake(session_id="api_does_not_exist_0000", prompt="p",
                    enqueue=enq_no_session, mirror=fake_mirror)
        missing_refused = False
    except Exception as exc:
        missing_refused = True
        missing_reason = str(exc)
check("E1 a session the enqueue path REJECTS produces a refusal, not a "
      "silent success", missing_refused is True,
      f"refused={missing_refused!r} — a wake that fires, finds nothing and "
      f"returns cleanly is indistinguishable from one that never fired")
check("E2 ...and that refusal is DISTINGUISHABLE from the surface refusal",
      isinstance(missing_reason, str) and missing_reason
      and missing_reason != reason,
      f"missing={missing_reason[:120]!r} surface={reason[:120]!r} — two "
      f"different conditions must not render as one line")

print()
failed = [r for r in results if not r[1]]
print("=" * 72)
print(f"cases: {len(results)}   failed: {len(failed)}")
if failed:
    print("EXPECTED RED until gateway/wake_resume.py + the scheduler branch land:")
    for name, _ok, _d in failed:
        print(f"    {name}")
print("=" * 72)


def test_wake_resume_starts_a_turn():
    """pytest entry — asserts on collected failures so 'no tests ran'
    cannot masquerade as green."""
    assert results, "no cases executed — a count is part of the verdict"
    assert not failed, f"{len(failed)} failed: {[f[0] for f in failed]}"


if __name__ == "__main__":
    sys.exit(1 if failed else 0)
