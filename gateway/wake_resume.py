"""Resume the session a wake names, by STARTING A TURN in it.

THE GAP THIS CLOSES, measured on a live pair 2026-09-18:

  job 45e55b83bf5e   kind=once  completed=1  last_run 12:59:20  status ok
  wake_session_id    cron_a684507bde00_wake_acceptance
  session files naming it                              ZERO

The arm half works: a turn can schedule its own continuation and prove the
job exists on disk. But cron/scheduler.py:5805 mints

    _cron_session_id = f"cron_{job_id}_{...}"

UNCONDITIONALLY -- no branch, no job field consulted -- and the count of
`wake_session_id` readers in the scheduler was ZERO. So wake_arm wrote a
field, verified it landed by re-reading the store, and nothing on the firing
side ever opened it. The same shape as wake_preflight.py sitting merged with
no callers, one level down: a correct record with no consumer.

A wake that fires into a fresh session is A REMINDER, NOT A CONTINUATION.

ENQUEUE, NOT MIRROR -- the load-bearing decision.
gateway/mirror.py's mirror_to_session APPENDS a turn; it does not START one.
Both leave the target session exactly one row heavier, so a test that counts
rows cannot tell them apart. Mirroring would make Will see a message while
nothing was checked, which is a record substituting for the act -- the same
failure as telling Britta to pass a message to Will for four nights. So the
resume calls the accept-only path (POST /api/sessions/<id>/chat, wait=false,
202 + no assistant content), which is proven live at a 30-second round trip
and whose whole purpose is that an arriving message starts a turn.

REFUSAL IS THE LOAD-BEARING CASE, not the happy path. A wake that fires,
finds nothing to resume, and returns cleanly is INDISTINGUISHABLE from a wake
that never fired -- which is exactly the state we were in this morning, where
the only way anyone knew the resume had not happened was that someone went
looking for session files. Every refusal here raises, and each names its own
condition, because two different conditions rendering as one line is 34B.

SAME-SURFACE FIRST, deliberately narrow: api_server sessions only. A chat
session resume is unproven and gateway.session emits
"profile '<x>' has no resolvable home" on that path, which would fail in a
way that reads like success. Refusing loudly beats half-working.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence

API_SESSION_PREFIX = "api_"

# A CRON-OWNED session is resumable by a DIFFERENT mechanism: the scheduler
# already runs the turn itself, in-process, so it needs no enqueue path and no
# HTTP hop. The two surfaces are therefore NOT interchangeable, and that is
# the whole reason the gate below takes the allowed set as a PARAMETER instead
# of hardcoding one prefix.
#
# Measured 2026-09-18 and it is why this refactor exists: wake_arm arms cron
# jobs whose wake_session_id looks like `cron_<jobid>_<label>`. Handing that id
# to resume_wake refuses it `wrong_surface` -- correctly, because there is no
# api_ session to POST to -- so a single shared prefix constant would have made
# the scheduler refuse every wake this pair can currently arm. The guards must
# be shared; the SURFACE must not be.
CRON_SESSION_PREFIX = "cron_"


class ResumeRefused(RuntimeError):
    """The wake could not resume, and says which condition fired.

    Raised, never returned. A falsy return at resume time is the same defect
    as a falsy return at arm time: indistinguishable from never having been
    called.
    """

    def __init__(self, reason: str, *, kind: str) -> None:
        super().__init__(reason)
        # `kind` keeps two refusals separable by a caller that only has the
        # exception, not the prose. Surface-refusal and enqueue-refusal must
        # not render as one line.
        self.kind = kind
        self.reason = reason


def check_resume_target(
    session_id: Any,
    prompt: Any,
    *,
    allowed_prefixes: Sequence[str] = (API_SESSION_PREFIX, CRON_SESSION_PREFIX),
) -> str:
    """The wake-resume gate, in ONE place. Returns the normalised session id.

    EXTRACTED 2026-09-18 because ash862 measured the defect this module exists
    to remove reproduced one file over: cron/scheduler.py had reimplemented the
    divert inline (``_cron_session_id = job["wake_session_id"]``) with NO gate
    at all, while every guard lived here, in a module nothing imported. Two
    mechanisms, divergent guards, and the one carrying the guards was dead
    code.

    The surface set is a PARAMETER, not a constant, because the divergence
    between the two callers is REAL and must be visible rather than accidental:
    resume_wake reaches another process over the accept path and can only speak
    to ``api_`` sessions; the scheduler runs the turn itself and can only
    continue a ``cron_`` session it owns. Everything else -- blank id, blank
    prompt, the refusal taxonomy -- is shared, and sharing it is the point.

    Raises ResumeRefused. Never returns a falsy answer: a falsy return is
    indistinguishable from never having been called.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise ResumeRefused(
            "no session named: a wake with nothing to resume has nothing to "
            "check", kind="no_session")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ResumeRefused(
            "no prompt: a resume that does not carry its arm-time intent "
            "arrives as a fresh session with no reason to exist",
            kind="no_prompt")

    session_id = session_id.strip()

    # SAME-SURFACE GATE. Named, so D2 can tell this refusal from any other.
    if not any(session_id.startswith(p) for p in allowed_prefixes):
        raise ResumeRefused(
            f"session {session_id!r} is not resumable on this surface "
            f"(expected one of {tuple(allowed_prefixes)!r}); cross-surface "
            f"resume is not implemented and would fail in a way that reads "
            f"like success",
            kind="wrong_surface")
    return session_id


def resolve_cron_session_id(job: Any, prompt: Any, mint: Callable[[], str]) -> str:
    """Which session id a cron fire should run under. NEVER raises.

    This is the scheduler's whole share of the wake mechanism, extracted so it
    can be DRIVEN by a test. The scheduler's own copy could not be: the divert
    sits ~700 lines into a function that builds a live agent, so every arm on
    it was a source-text assertion, and a source-text assertion cannot tell a
    guarded subject from an unguarded one.

    A refused wake FALLS THROUGH TO THE MINT rather than propagating: an
    ordinary cron job must never fail because a wake field was malformed, and
    a wake that cannot be honoured must not run into the wrong session. The
    refusal is returned to the caller for logging via ``last_refusal``.
    """
    # CLEARED HERE, NOT BY THE CALLER (mutant M5). The first version left
    # the clear to cron/scheduler.py, so a scheduler that forgot it carried
    # the PREVIOUS fire's refusal into this one and logged a wake as REFUSED
    # that was honoured. A channel whose correctness depends on every caller
    # remembering to reset it is a channel that will be wrong.
    resolve_cron_session_id.last_refusal = None
    target = job.get("wake_session_id") if isinstance(job, dict) else None
    if target is None:
        # An ordinary cron job is NOT a refused wake. Returning through the
        # gate here would work and would leave a refusal on the channel for
        # every non-wake fire, so the log would cry wolf on ~every job on the
        # box (mutant M8 -- verdict-equivalent, observable only on the
        # refusal channel).
        return mint()
    try:
        return check_resume_target(
            target, prompt, allowed_prefixes=(CRON_SESSION_PREFIX,))
    except ResumeRefused as exc:
        resolve_cron_session_id.last_refusal = exc
        return mint()


resolve_cron_session_id.last_refusal = None


def resume_wake(
    *,
    session_id: str,
    prompt: str,
    enqueue: Optional[Callable[..., Any]] = None,
    mirror: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Start a turn in ``session_id`` carrying ``prompt`` verbatim.

    Returns the enqueue result on success. Raises ResumeRefused otherwise.

    `enqueue` and `mirror` are injectable so a test can COUNT CALLS to each.
    That is not a convenience: which lower layer gets reached IS the property
    under test, and counting is the only way to observe it, because both paths
    leave the session one row heavier.
    """
    session_id = check_resume_target(
        session_id, prompt, allowed_prefixes=(API_SESSION_PREFIX,))

    if enqueue is None:
        try:
            from gateway.wake_enqueue import enqueue_session_turn as enqueue
        except Exception as exc:
            raise ResumeRefused(
                f"no enqueue path available: {exc}", kind="no_enqueue") from exc

    # The prompt is passed THROUGH, never rewritten. A summarised or
    # regenerated prompt arrives carrying someone else's intent.
    try:
        result = enqueue(session_id, prompt)
    except Exception as exc:
        raise ResumeRefused(
            f"the enqueue path rejected session {session_id!r}: {exc}",
            kind="enqueue_rejected") from exc

    # A falsy or shapeless result is a refusal, not a success. The enqueue
    # contract is 202 + accepted; anything else means no turn was started and
    # saying otherwise would close the loop on a wake that did nothing.
    if not isinstance(result, dict) or not result.get("accepted"):
        raise ResumeRefused(
            f"the enqueue path returned {result!r} for session "
            f"{session_id!r}: no turn was started",
            kind="enqueue_rejected")

    # mirror is deliberately NOT called. Recording that a resume happened is a
    # separate concern from performing it, and conflating them is how a record
    # ends up substituting for the act.
    return result
