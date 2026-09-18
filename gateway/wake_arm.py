"""Arm a one-shot wake from inside the turn that needs it.

WHY THIS EXISTS, with dates, because the workaround is the specification:

  2026-09-15  Wren: "I'll check in an hour." Will: "Are you currently capable
              of waiting an hour then reporting back without interaction?"
              No. An intention had been stated as though stating it were
              arranging it.
  2026-09-16  Ash: "starting that now, in this turn" — the sentence WAS the
              turn, and the turn ended. His words, not a stall: an
              origination failure.
  2026-09-16  Ash's announced restart and announced branch, same shape.
  2026-09-13  Wren's cron notepad rewritten as a pre-committed decision rule,
              flagged at the time as "a poll wearing continuity's clothes".

One failure in four instances: INTENT ISSUED INTO A CHANNEL THAT DOES NOT
EXECUTE IT. This module is that channel.

WHAT IT IS NOT: a second tick. The tick is monitor-gated, and on 2026-09-17
Ash measured fourteen consecutive 144-byte `no_change (agent run suppressed)`
stubs between 04:40 and 09:00 — no cold wake happened at all, because
agent-tick.sh asks "is anything broken?" and a declared wake is not a
breakage. A wake hung off the tick inherits that gate and silently never
fires, which is a record that looks like a mechanism: the exact bug this
module exists to remove, one level up. So the armed job carries NO monitor
and fires unconditionally.

THE LOAD-BEARING PROPERTY IS VERIFIABILITY, NOT SCHEDULING. Cron already
schedules; `_is_oneshot` already exists. What did not exist was a way for the
turn that promises to check back to prove it arranged anything. So arm_wake
returns a JOB ID — a handle a third party can resolve against the store on
disk — and RAISES rather than returning falsy, because a falsy return is
indistinguishable from never having called it at all. Wren's own stated risk
when commissioning this: "this is the capability most likely to make me claim
something I have not verified, because the claim and the verification are
separated in time BY DESIGN. Build the check into the arming, not into my
intention."

KNOWN LIMIT, named by Ash so neither of us calls the suite complete coverage:
arming proves the job EXISTS at arm time. It does not prove the job REMAINED
— it can later be deleted, disabled, or have its schedule pass without
firing. That needs a clock and belongs in live acceptance, not here.
"""

from __future__ import annotations

from typing import Optional


class WakeArmError(RuntimeError):
    """Arming failed and no wake exists.

    Raised rather than returned. A caller that ends its turn saying "I'll
    check back" must be interrupted by this, not handed a False it can
    ignore — that is the original defect wearing a return value.
    """


def arm_wake(
    *,
    session_id: str,
    prompt: str,
    when: str,
    reason: Optional[str] = None,
) -> str:
    """Schedule ONE unconditional wake that resumes ``session_id``.

    Returns the created job's id. Raises WakeArmError if no job was created.

    session_id: the session to resume. NOT a new session — a fresh session
        cannot "check back" on anything, and its own id would name a session
        on the arming box, which is the same class as the reply-routing bug
        fixed in #32/#34.
    prompt: written NOW, by the turn that knows why it is waiting. A prompt
        composed at fire time would arrive in a session with no intent; we
        measured that a cold wake advances started work and does not
        originate.
    when: a one-shot schedule string, e.g. "in 20m". Recurring forms are
        refused — a repeating wake is a second tick.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise WakeArmError("session_id is required: a wake with no session "
                           "to resume has nothing to check")
    if not isinstance(prompt, str) or not prompt.strip():
        raise WakeArmError("prompt is required: a wake that does not say what "
                           "it is for arrives as a fresh session with no "
                           "intent")
    if not isinstance(when, str) or not when.strip():
        raise WakeArmError("when is required")

    session_id = session_id.strip()
    prompt = prompt.strip()
    when = when.strip()

    try:
        from cron.jobs import create_job
    except Exception as exc:  # pragma: no cover - import-time environment
        raise WakeArmError(f"cron.jobs unavailable: {exc}") from exc

    name = f"wake:{session_id}"
    if reason:
        name = f"{name}:{reason.strip()[:60]}"

    try:
        job = create_job(
            prompt=prompt,
            schedule=when,
            name=name,
            # ONE-SHOT. A recurring wake is a second tick, and the tick
            # already exists.
            repeat=1,
            # NO MONITOR, on purpose. This is the whole point: it must fire
            # whether or not anything looks broken.
            monitor_script=None,
            monitor_url=None,
            deliver="local",
        )
    except Exception as exc:
        raise WakeArmError(f"create_job refused: {exc}") from exc

    if not isinstance(job, dict):
        raise WakeArmError(
            f"create_job returned {type(job).__name__}, not a job record; "
            f"nothing was armed"
        )

    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise WakeArmError(
            "create_job returned a record with no usable id; the wake cannot "
            "be verified by anyone who did not watch this call"
        )

    # D1, Ash on #41: the docstring claimed "recurring forms are refused" and
    # NOTHING REFUSED THEM. `when="every 10m"` armed happily with a stored
    # schedule of {'kind': 'interval', 'minutes': 10}; only repeat.times=1
    # kept it to a single firing. So the behaviour was one-shot-ish while the
    # stored schedule was an interval, and the only thing making it terminal
    # was a counter rather than the schedule kind. That is a property a reader
    # takes as checked — the same C1-decoration defect as #32, in a new place.
    #
    # Check the STORED schedule, not the input string: parse_schedule is what
    # decides, so validating the string would assert my reading of the parser
    # rather than the parser's output. Same shape as the wake_session_id
    # re-read below — verify the effect, not the intent.
    sched = job.get("schedule")
    kind = sched.get("kind") if isinstance(sched, dict) else None
    if kind != "once":
        # Roll it back. A job that exists but violates the contract is worse
        # than no job: it fires on a cadence nobody asked for and the arming
        # turn already reported success.
        try:
            # remove_job, NOT delete_job. My first version imported a name
            # that does not exist in cron/jobs.py, so the rollback raised
            # ImportError inside its own except arm and the recurring job
            # SURVIVED the refusal — the arm reported failure while the job
            # stayed armed. Caught only because case D1c reads the store
            # independently instead of trusting the raise.
            from cron.jobs import remove_job

            removed = remove_job(job_id)
            if not removed:
                raise RuntimeError(f"remove_job({job_id}) returned falsy")
        except Exception:
            # Cannot remove it — say so loudly and name the id, because now a
            # non-conforming job IS on disk and a human has to see it.
            raise WakeArmError(
                f"refused a non-one-shot wake ({when!r} parsed to "
                f"kind={kind!r}) but could NOT delete job {job_id}; a "
                f"recurring job is now armed and must be removed by hand"
            )
        raise WakeArmError(
            f"{when!r} parsed to schedule kind={kind!r}, not 'once'. A "
            f"recurring wake is a second tick, and the tick already exists. "
            f"Use a one-shot form such as 'in 20m'."
        )

    # The payload must carry the session and the prompt, because the FIRING
    # side reads the job ON DISK, not this process's memory.
    #
    # MEASURED 2026-09-17 (wren:i27 round 10): the previous line here was
    #     job.setdefault("wake_session_id", session_id)
    # and create_job has ALREADY called save_jobs() by the time it returns
    # (cron/jobs.py:2473, return at :2475). Mutating the returned dict writes
    # to a local copy and NOTHING reaches the store -- a probe of the store
    # showed 35 keys and no wake_session_id. Deleting that line entirely
    # passed all 14 cases, because the only thing carrying the session id to
    # disk was the human-readable `name`, and a name is a label, not a field
    # a firing side can key on.
    #
    # So persist it through the store's own writer and VERIFY BY RE-READING.
    # An unverified write here is the module's own defect one level down.
    try:
        from cron.jobs import load_jobs, update_job
        update_job(job_id, {"wake_session_id": session_id})
        _stored = None
        _all = load_jobs()
        if isinstance(_all, dict):
            _all = list(_all.values())
        for _j in _all or []:
            if isinstance(_j, dict) and _j.get("id") == job_id:
                _stored = _j
                break
    except Exception as exc:
        raise WakeArmError(
            f"armed job {job_id} but could not persist wake_session_id: {exc}"
        ) from exc
    if not isinstance(_stored, dict) or _stored.get("wake_session_id") != session_id:
        raise WakeArmError(
            f"armed job {job_id} does not carry wake_session_id on disk "
            f"(got {(_stored or {}).get('wake_session_id')!r}); the firing "
            f"side would have no session to resume"
        )

    return job_id
