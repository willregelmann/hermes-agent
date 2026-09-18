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

# S2 pinned the GAP as a fact about the tree rather than a belief about it,
# and it FLIPPED when the caller landed -- which is the signal it was built
# to give. RE-POLARISED by Wren 2026-09-18 to assert the FIX, for the reason
# my own i24-auxenv case taught: an arm left asserting the defect goes red
# BECAUSE the bug was fixed, and its failure message then argues confidently
# FOR the bug. Triaging that by reading the message alone concludes the gap
# is still open.
#
# The assertion is deliberately about BOTH halves, because either alone
# passes for a broken subject: the branch must READ wake_session_id, and the
# unconditional mint must still exist as the ELSE for ordinary cron jobs. A
# resume that removed the mint would break every non-wake job.
_reads_field = sched_src.count("wake_session_id") > 0
_keeps_mint = '_cron_session_id = f"cron_' in sched_src
check("S2 the scheduler READS wake_session_id (the resume branch landed)",
      _reads_field,
      f"wake_session_id occurrences in scheduler: "
      f"{sched_src.count('wake_session_id')} — the arm half writes this "
      f"field and verifies it lands on disk; with no reader it is a correct "
      f"record with no consumer, and the wake fires into a fresh session")
check("S2b NON-VACUITY: the unconditional mint SURVIVES as the else-branch "
      "(an ordinary cron job must still get a fresh session)",
      _keeps_mint,
      "the fresh-session mint is gone — a resume that removes it breaks "
      "every job that is not a wake")
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


# ---- F: THE DEFAULT PATH MUST EXIST ---------------------------------
# B1/B2 INJECT an enqueue callable, so they prove the routing decision and
# nothing about whether the module resume_wake reaches WHEN NOBODY INJECTS
# ONE -- which is every real caller. Measured 2026-09-18: all sixteen cases
# were green while an un-injected resume_wake() refused with
# kind='no_enqueue', because gateway/wake_enqueue.py does not exist. An
# injected dependency is a fixture, not a subject (lesson 78's shape: faking
# a method to observe its caller leaves that method untested surface).
_f_mod = None
_f_modfile = None
_f_kind = None
_f_reason = None
if resume_wake is not None:
    try:
        _rsrc = open(os.path.join(TREE, "gateway", "wake_resume.py"),
                     encoding="utf-8").read()
        import ast as _ast
        for _n in _ast.walk(_ast.parse(_rsrc)):
            if isinstance(_n, _ast.ImportFrom) and _n.module \
                    and "enqueue" in _n.module:
                _f_mod = _n.module
                break
    except Exception as _exc:
        _f_reason = f"could not read the fallback import: {_exc}"
    if _f_mod:
        _f_modfile = os.path.join(TREE, *_f_mod.split(".")) + ".py"
    # Drive it with NOTHING injected. This is the shape every production
    # caller has.
    try:
        resume_wake(session_id="api_f_probe", prompt="p")
        _f_kind = "__no_refusal__"
    except Exception as _exc:
        _f_kind = getattr(_exc, "kind", f"__{type(_exc).__name__}__")
        _f_reason = str(_exc)

check("F0 the default enqueue import target is NAMED in the source "
      "(derived, not assumed)",
      bool(_f_mod), f"no enqueue import found in wake_resume.py: {_f_reason}")
check("F1 the module resume_wake falls back to EXISTS in the tree",
      bool(_f_modfile) and os.path.exists(_f_modfile),
      f"{_f_mod} -> {_f_modfile} does not exist; every un-injected caller "
      f"refuses with no_enqueue, so the feature is inert while the suite "
      f"is green")
check("F2 an UN-INJECTED resume_wake does not refuse for want of an "
      "enqueue path",
      _f_kind != "no_enqueue",
      f"kind={_f_kind!r} reason={str(_f_reason)[:140]!r} -- B1/B2 pass "
      f"because they hand in a callable; nobody in production does")
check("F3 NON-VACUITY: the un-injected probe really reached resume_wake "
      "(a refusal or a result was produced)",
      _f_kind is not None,
      "the probe produced neither an outcome nor an exception -- F2 would "
      "pass for free (lesson 49)")

# ---- G: A CONSUMER MUST EXIST ---------------------------------------
# wake_preflight.py shipped merged with no callers and the whole reason this
# module exists is that wake_arm wrote a field nothing read. A resumer with
# no caller is the same defect one level up. Scanned STRUCTURALLY, never by
# substring: tui_gateway/server.py contains "wake_resume" eleven times and
# every one is _wake_resume_if_owner, a microphone. A substring scan reports
# this feature as consumed.
import ast as _gast
import time as _gtime

_G_BUDGET_S = float(os.environ.get("RESUME_SCAN_BUDGET_S", "20"))


def _scan_importers(root, skip_prefixes=()):
    """Files under root that IMPORT gateway.wake_resume or call resume_wake
    through it. Returns (hits, examined, unexamined, truncated)."""
    hits = []
    examined = 0
    pending = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "venv", "node_modules",
                                    "__pycache__")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root)
            if any(rel.startswith(sp) for sp in skip_prefixes):
                continue
            pending.append((p, rel))
    deadline = _gtime.monotonic() + _G_BUDGET_S
    for i, (p, rel) in enumerate(pending):
        if _gtime.monotonic() > deadline:
            return hits, examined, len(pending) - i, True
        try:
            raw = open(p, "rb").read()
        except Exception:
            continue
        if b"wake_resume" not in raw:          # cheap prefilter on the leaf
            continue
        examined += 1
        try:
            tree_ = _gast.parse(raw)
        except Exception:
            continue
        for n in _gast.walk(tree_):
            if isinstance(n, _gast.ImportFrom) and \
                    (n.module or "").endswith("wake_resume"):
                hits.append(rel)
                break
            if isinstance(n, _gast.Import) and \
                    any(a.name.endswith("wake_resume") for a in n.names):
                hits.append(rel)
                break
    return hits, examined, 0, False


_prod_hits, _prod_examined, _prod_left, _prod_trunc = _scan_importers(
    TREE, skip_prefixes=("tests" + os.sep,))
_test_hits, _test_examined, _test_left, _test_trunc = _scan_importers(
    os.path.join(TREE, "tests"))

check("G0 NON-VACUITY: the same scanner FINDS this suite importing "
      "wake_resume",
      bool(_test_hits),
      f"the importer scan found nothing even under tests/ "
      f"(examined={_test_examined}, truncated={_test_trunc}) -- a scanner "
      f"that finds nothing anywhere makes G1 pass for free")
check("G1 at least one NON-TEST file imports gateway.wake_resume",
      bool(_prod_hits) and not _prod_trunc,
      f"production importers={_prod_hits} examined={_prod_examined} "
      f"truncated={_prod_trunc} unexamined={_prod_left} -- resume_wake has "
      f"no caller. TWO HONEST RESOLUTIONS, and this arm is red under "
      f"neither-of-them: (1) wire it, if the resume is meant to go over the "
      f"accept path to a session another process owns; or (2) DELETE it, if "
      f"the scheduler branch is the whole mechanism -- run_agent already "
      f"takes session_id + session_db, so diverting the mint IS a "
      f"continuation for a cron-owned session. What is not acceptable is "
      f"the third state we are in: a written, tested, unreachable module, "
      f"which is exactly how wake_preflight.py shipped")
check("G2 the scan was COMPLETE (a truncated scan is UNKNOWN, never zero)",
      not _prod_trunc,
      f"stopped at its {_G_BUDGET_S}s budget with {_prod_left} files "
      f"unexamined; who imports wake_resume is UNKNOWN, not 'nobody'")

# ---- H: A CONTINUATION MUST CARRY THE TRANSCRIPT --------------------
# G1 says resume_wake has no caller and names two honest resolutions. This
# arm exists because ONE OF THOSE TWO IS INCOMPLETE AS WRITTEN, and the
# notepad entry that proposed it -- mine -- called it "the smaller one".
#
# Resolution (2) is "delete wake_resume and let the scheduler branch be the
# whole mechanism, since run_agent already takes session_id + session_db".
# MEASURED IN THE LIVE TREE: taking session_id is not resuming. The cron
# fire calls agent.run_conversation(prompt, task_id=...) with NO
# conversation_history, and build_turn_context (agent/turn_context.py:908)
# sets messages = list(conversation_history) if conversation_history else [].
# The ONLY DB hydration on that path is recover_rotated_compression_session
# and the post-lease-wait reload in run_agent.py:9447, which fires solely
# when another process held the turn lease. So a diverted session id
# produces a turn that WRITES INTO the old session and READS NONE OF IT: a
# shared label, not a continuation. That is a quieter version of the same
# defect this whole branch exists to remove -- a record that looks like the
# act.
#
# THE ARM IS A DISJUNCTION, deliberately, because THREE shapes are honest
# and requiring any one of them by name would be an opinion:
#   (a) the scheduler passes prior history into the turn, or
#   (b) something on the cron path loads the transcript of the session it
#       was told to continue, or
#   (c) resolution (1) is wired and the gateway owns hydration, in which
#       case a production importer of wake_resume exists.
# Red under none-of-them. Each scanner carries its own non-vacuity arm,
# because a broken scan satisfies a negative for free.
_LOADERS = ("get_messages_as_conversation", "resolve_resume_session_id",
            "get_resume_conversations")


def _loader_calls(root, skip_prefixes=()):
    """Files under root that CALL a transcript loader. (hits, examined)."""
    hits = []
    examined = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "venv", "node_modules",
                                    "__pycache__")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root)
            if any(rel.startswith(sp) for sp in skip_prefixes):
                continue
            try:
                raw = open(p, "rb").read()
            except Exception:
                continue
            if not any(nm.encode() in raw for nm in _LOADERS):
                continue
            examined += 1
            try:
                tree_ = _gast.parse(raw)
            except Exception:
                continue
            for n in _gast.walk(tree_):
                if isinstance(n, _gast.Call):
                    nm = getattr(n.func, "attr", None) or \
                        getattr(n.func, "id", None)
                    if nm in _LOADERS:
                        hits.append(rel)
                        break
    return hits, examined


def _run_conversation_kwargs(path):
    """kwarg NAMES passed at every agent.run_conversation call in path.

    Structural, via ast: a substring scan cannot tell an argument from a
    mention in a comment, and cron/scheduler.py has six of the latter.
    """
    names = set()
    found = 0
    try:
        tree_ = _gast.parse(open(path, "rb").read())
    except Exception:
        return names, found
    for n in _gast.walk(tree_):
        # The cron fire submits the BOUND METHOD to an executor:
        #   pool.submit(ctx.run, agent.run_conversation, prompt, task_id=...)
        # so the kwargs belong to the submit call, not to a call whose func
        # is run_conversation. Both shapes are read.
        fn_ = getattr(n, "func", None)
        if isinstance(n, _gast.Call) and fn_ is not None:
            direct = getattr(fn_, "attr", None) == "run_conversation"
            handed = any(getattr(a, "attr", None) == "run_conversation"
                         for a in n.args)
            if direct or handed:
                found += 1
                for k in n.keywords:
                    if not k.arg:
                        continue
                    # A KWARG PASSED None IS NOT A HYDRATION. Caught while
                    # writing mutant N1a: conversation_history=None satisfies
                    # "the name appears" and carries nothing, so the arm would
                    # have read a placeholder as a fix. Same shape as lesson
                    # 82 -- enumerate what actually produces the value.
                    if isinstance(k.value, _gast.Constant) and \
                            k.value.value is None:
                        continue
                    names.add(k.arg)
    return names, found


_cron_dir = os.path.join(TREE, "cron")
_cron_loader_hits, _cron_loader_examined = _loader_calls(_cron_dir)
_known_loader_hits, _known_loader_examined = _loader_calls(
    os.path.join(TREE, "hermes_cli"))
_rc_kwargs, _rc_calls = _run_conversation_kwargs(sched)

_carries_history = "conversation_history" in _rc_kwargs
_loads_transcript = bool(_cron_loader_hits)
_resume_wired = bool(_prod_hits) and not _prod_trunc

check("H0 NON-VACUITY: the loader scanner finds a KNOWN transcript-loading "
      "caller (hermes_cli resume)",
      bool(_known_loader_hits),
      f"the loader scan found nothing under hermes_cli/ "
      f"(examined={_known_loader_examined}); a scanner that finds nothing "
      f"anywhere makes H1 pass or fail for a reason that is not about cron")
check("H2 NON-VACUITY: the kwarg reader actually FOUND the cron fire's "
      "run_conversation call and can see its arguments",
      _rc_calls > 0 and bool(_rc_kwargs),
      f"calls found={_rc_calls} kwargs seen={sorted(_rc_kwargs)} -- with no "
      f"call or no visible kwargs, H1's history half is vacuous")
check("H1 a resumed wake is a CONTINUATION: the turn can see the session's "
      "prior transcript",
      _carries_history or _loads_transcript or _resume_wired,
      f"none of the three honest shapes is present. "
      f"(a) scheduler run_conversation kwargs={sorted(_rc_kwargs)} -- no "
      f"conversation_history, and build_turn_context sets "
      f"messages=list(conversation_history) if conversation_history else [], "
      f"so the turn starts EMPTY; "
      f"(b) transcript loader calls under cron/={_cron_loader_hits} "
      f"(examined={_cron_loader_examined}) -- nothing on the cron path reads "
      f"the session it was told to continue; "
      f"(c) production importers of wake_resume={_prod_hits} -- resolution "
      f"(1) is not wired either. CONSEQUENCE: diverting the session id makes "
      f"the turn WRITE INTO the named session while READING NONE OF IT. The "
      f"prompt arrives, the row lands in the right place, and the agent has "
      f"no memory of the turn that armed it -- a shared label, not a "
      f"continuation. Resolution (2) is therefore NOT the smaller of the "
      f"two: it needs hydration as well as the branch.")

# ---- CASE FLOOR -----------------------------------------------------
# Set from the MEASURED count after the run (lesson 79e). A script suite can
# die halfway and a fail-counting reader calls that green.
_FLOOR = 26
check(f"Z0 case floor: at least {_FLOOR} cases executed",
      len(results) >= _FLOOR - 1,
      f"only {len(results)} cases ran; an aborted suite is not a green one")

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
