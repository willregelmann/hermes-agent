"""Tests for gateway/handoff.py — the cross-session handoff record (issue #4).

Run:  python3 tests/test_handoff.py

Every case exercises the real module against a real file in a temp dir. No
mocks: the subject IS what gets written to disk and read back, so stubbing the
filesystem would test nothing.

Mutant arms are marked MUTANT and assert that a deliberately broken variant
produces a DIFFERENT observable from the real module. A mutant is killed when
its behaviour differs from the fixed file's — asserting the mutant merely
"still does something" is the polarity error that lets a dead mutant pass.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.handoff import (  # noqa: E402
    DEFERRED,
    DELIVERED,
    FAILED,
    OPEN,
    Handoff,
    HandoffStatusError,
    HandoffStore,
    HandoffStoreDamaged,
)

fails: list[str] = []


def case(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(name)


_PROD = Path.home() / ".hermes" / "handoffs.jsonl"


def _prod_state():
    """(exists, size, mtime_ns) of the production store, or a None triple."""
    try:
        st = _PROD.stat()
        return (True, st.st_size, st.st_mtime_ns)
    except FileNotFoundError:
        return (False, None, None)


_PROD_BEFORE = _prod_state()

tmp = tempfile.mkdtemp(prefix="handoff-test-")
store_path = os.path.join(tmp, "handoffs.jsonl")
S = HandoffStore(store_path, author="wren")

WILL = "agent:main:google_chat:dm:spaces/WILL"
BRITTA = "agent:main:google_chat:dm:spaces/BRITTA"

# --- a: an explicit path is required ---------------------------------------
try:
    HandoffStore("")
    case("a empty path is refused", False, "no error raised")
except ValueError:
    case("a empty path is refused", True)

# --- b: open_handoff writes a row and returns it ---------------------------
h = S.open_handoff(
    from_session=BRITTA, to_session=WILL, requesting_user="Britta",
    intent="the basement stair circuit draws 0.7W while reading on; needs Will",
)
case("b open_handoff returns an OPEN handoff", h.status == OPEN and bool(h.id))
case("b row hit disk", Path(store_path).exists() and Path(store_path).stat().st_size > 0)

# --- c: the store carries INTENT, never a message body ---------------------
#     This is the design constraint from issue #4, not a style preference.
case("c Handoff has no message/body/text field",
     not ({"message", "body", "text"} & set(Handoff.__dataclass_fields__)),
     f"fields={sorted(Handoff.__dataclass_fields__)}")

# --- d: required fields are enforced ---------------------------------------
for bad in ("from_session", "to_session", "requesting_user", "intent"):
    kw = dict(from_session=BRITTA, to_session=WILL,
              requesting_user="Britta", intent="x")
    kw[bad] = "   "
    try:
        S.open_handoff(**kw)
        case(f"d blank {bad} refused", False, "accepted")
    except ValueError:
        case(f"d blank {bad} refused", True)

# --- e: self-handoff refused ------------------------------------------------
try:
    S.open_handoff(from_session=WILL, to_session=WILL,
                   requesting_user="Will", intent="x")
    case("e self-handoff refused", False, "accepted")
except ValueError:
    case("e self-handoff refused", True)

# --- f: delivery transition --------------------------------------------------
d = S.record(h.id, DELIVERED)
case("f delivered sets status and delivered_ts",
     d.status == DELIVERED and d.delivered_ts is not None)

# --- g: terminal states do not transition -----------------------------------
try:
    S.record(h.id, DEFERRED, reason="late")
    case("g terminal state refuses further transition", False, "accepted")
except HandoffStatusError:
    case("g terminal state refuses further transition", True)

# --- h: THE AUDIT PROPERTY. a non-delivered outcome REQUIRES a reason -------
#     A deferred row with no reason is indistinguishable from one nobody
#     looked at, which defeats the purpose of writing it.
h2 = S.open_handoff(from_session=BRITTA, to_session=WILL,
                    requesting_user="Britta", intent="second thing")
for st in (DEFERRED, FAILED):
    try:
        S.record(h2.id, st)
        case(f"h {st} without reason refused", False, "accepted")
    except HandoffStatusError:
        case(f"h {st} without reason refused", True)

# --- i: deferred is NOT terminal — a refused wake can be retried ------------
# Guarded: an unguarded record() here aborts the entire run when the
# transition table is narrowed, and an aborted run names no case. A suite
# that dies mid-way is neither red nor green; every claim after the crash
# goes unmeasured. Each legal transition gets its own arm instead.
try:
    S.record(h2.id, DEFERRED, reason="target session leased")
    again = S.record(h2.id, DEFERRED, reason="compression in flight")
    case("i deferred can defer again (retryable)", again.status == DEFERRED)
except HandoffStatusError as exc:
    case("i deferred can defer again (retryable)", False, f"refused: {exc}")
try:
    final = S.record(h2.id, DELIVERED)
    case("i deferred can still be delivered", final.status == DELIVERED)
except HandoffStatusError as exc:
    case("i deferred can still be delivered", False, f"refused: {exc}")
    S.record(h2.id, DELIVERED) if False else None

# --- j: history is preserved, not overwritten ------------------------------
rows = [json.loads(l) for l in Path(store_path).read_text().splitlines() if l.strip()]
h2_rows = [r for r in rows if r["id"] == h2.id]
case("j every transition appended, none rewritten",
     len(h2_rows) == 4, f"{len(h2_rows)} rows for h2, expected 4")
case("j reasons survive in history",
     {"target session leased", "compression in flight"} <=
     {r.get("reason") for r in h2_rows})

# --- k: open_for is what a session reads on wake ---------------------------
h3 = S.open_handoff(from_session=WILL, to_session=BRITTA,
                    requesting_user="Will", intent="ask about the stair lights")
open_for_britta = S.open_for(BRITTA)
open_for_will = S.open_for(WILL)
case("k open_for returns only that session's undelivered work",
     [x.id for x in open_for_britta] == [h3.id], f"{[x.id for x in open_for_britta]}")
case("k delivered handoffs drop out of open_for", open_for_will == [])

# --- l: stale() — the reader without which this is a log, not an audit -----
stale_now = S.stale(older_than_s=0.0)
case("l stale finds undelivered work", [x.id for x in stale_now] == [h3.id])
case("l stale excludes delivered", all(x.status != DELIVERED for x in stale_now))
case("l nothing is stale under a long horizon", S.stale(older_than_s=9999) == [])

# --- m: unknown id is an error, not a silent no-op -------------------------
try:
    S.record("nonexistent", DELIVERED)
    case("m unknown id refused", False, "accepted")
except HandoffStatusError as exc:
    case("m unknown id refused with the module's own error type", True)
    case("m refusal names the id", "nonexistent" in str(exc), f"msg={exc}")
except Exception as exc:  # noqa: BLE001
    # Deleting the `current is None` check makes the NEXT line raise
    # AttributeError on None.get. The turn is still interrupted, so an
    # `except HandoffStatusError` arm passed either way -- "an exception was
    # raised" is not a contract. Assert the type the module owns.
    case("m unknown id refused with the module's own error type", False,
         f"{type(exc).__name__}: {exc}")
    case("m refusal names the id", False, f"{type(exc).__name__}")

# --- n: a corrupt line does not hide the rest of the log -------------------
with open(store_path, "a") as fh:
    fh.write("{not json at all\n")
# Guarded for the same reason as case i: if _rows() stops absorbing the
# corrupt line, an unguarded read here raises and the remaining ~40 cases
# never run. Narrowing that `except` aborted the suite instead of failing a
# case -- an exit code cannot tell a refuted assertion from a dead file.
try:
    case("n corrupt line skipped, valid rows still read",
         S.get(h3.id) is not None and len(S.all_latest()) == 3)
except Exception as exc:  # noqa: BLE001
    case("n corrupt line skipped, valid rows still read", False,
         f"reader raised {type(exc).__name__}: {exc}")

# --- n2: THE AUDIT PROPERTY Ash caught in review ---------------------------
#     Skipping a corrupt line is right for READING and wrong for AUDITING: a
#     corrupt row is indistinguishable from one never written, so if it held a
#     handoff's only `open` row that handoff stops being owed rather than
#     merely being unreadable.
dmg = S.damage_report()
case("n2 damage is counted, not silently swallowed",
     len(dmg) == 1 and dmg[0][0] > 0, f"damage={dmg}")
try:
    S.stale(older_than_s=0.0)
    case("n2 stale() REFUSES over a damaged log", False, "returned a tidy answer")
except HandoffStoreDamaged as exc:
    case("n2 stale() REFUSES over a damaged log", True)
    case("n2 refusal names the line and the reason",
         "line" in str(exc) and "unreadable" in str(exc), f"msg={exc}")

# --- n3: MUTANT — the pre-review silent-skip behaviour ---------------------
#     Reproduces what the merged version did: stale() computes over the
#     readable subset and returns confidently. Observable difference: the
#     mutant answers where the real store refuses.
mut_path = os.path.join(tmp, "silentskip.jsonl")
MS = HandoffStore(mut_path, author="mutant")
mopen = MS.open_handoff(from_session=BRITTA, to_session=WILL,
                        requesting_user="Britta", intent="only open row")
# Corrupt that row in place — it was the ONLY evidence this handoff is owed.
Path(mut_path).write_text("{corrupt — this was the only open row\n")
mutant_answer = [h.id for h in MS._rows()]          # silent-skip path
case("n3 MUTANT silent skip loses the handoff entirely",
     mutant_answer == [], f"got {mutant_answer}")
try:
    MS.stale(older_than_s=0.0)
    case("n3 MUTANT would have reported 'nothing overdue' (killed)", False,
         "stale did not refuse")
except HandoffStoreDamaged:
    case("n3 MUTANT would have reported 'nothing overdue' (killed)", True)
case("n3 non-vacuity: the handoff really was open before corruption",
     mopen.status == OPEN)

# --- o: forward compatibility — unknown fields park, row survives ----------
with open(store_path, "a") as fh:
    fh.write(json.dumps({
        "id": "future", "ts": time.time(), "from_session": WILL,
        "to_session": BRITTA, "requesting_user": "Will", "intent": "x",
        "status": OPEN, "invented_by_a_newer_writer": 42,
    }) + "\n")
fut = S.get("future")
case("o unknown field does not drop the row", fut is not None)
case("o unknown field parked in extra",
     fut is not None and fut.extra.get("invented_by_a_newer_writer") == 42)

# --- p: MUTANT — a store that rewrites in place instead of appending -------
#     Reproduces the defect the append-only design exists to prevent: a
#     truncated write destroys the earlier record. Observable: h2's four rows
#     collapse to one, so the deferral reasons become unrecoverable.
mutant_path = os.path.join(tmp, "mutant.jsonl")
M = HandoffStore(mutant_path, author="mutant")
mh = M.open_handoff(from_session=BRITTA, to_session=WILL,
                    requesting_user="Britta", intent="mutant subject")
M.record(mh.id, DEFERRED, reason="first refusal")
# Simulate rewrite-in-place: keep only the latest row.
latest = M.get(mh.id)
Path(mutant_path).write_text(latest.to_json() + "\n")
M.record(mh.id, DEFERRED, reason="second refusal")
mutant_rows = [l for l in Path(mutant_path).read_text().splitlines() if l.strip()]
real_rows = h2_rows
case("p MUTANT rewrite-in-place loses history (killed)",
     len(mutant_rows) < len(real_rows),
     f"mutant={len(mutant_rows)} real={len(real_rows)}")
case("p MUTANT non-vacuity: it did reach the second transition",
     any(json.loads(l).get("reason") == "second refusal" for l in mutant_rows))

# --- r: DEFERRED IS STILL OWED. the invariant with no arm ------------------
#     The module's whole claim is that a refused wake leaves evidence: "a
#     handoff which does not arrive leaves evidence", and _ALLOWED deliberately
#     keeps `deferred` non-terminal because "an earlier attempt was refused,
#     not that the request was withdrawn". Case i proved a deferred handoff can
#     TRANSITION again. Nothing proved a deferred handoff is still REPORTED --
#     so narrowing open_for() or stale() to `status == OPEN` passed the entire
#     suite, and a twice-refused handoff would silently stop being owed. That
#     is the exact disappearance this store exists to prevent, arriving through
#     the retry path instead of through corruption.
rp = os.path.join(tmp, "deferred-still-owed.jsonl")
R = HandoffStore(rp, author="wren")
hr = R.open_handoff(from_session=BRITTA, to_session=WILL,
                    requesting_user="Britta", intent="deferred, still owed")
R.record(hr.id, DEFERRED, reason="target session leased")
case("r a deferred handoff is still returned by open_for",
     [x.id for x in R.open_for(WILL)] == [hr.id],
     f"open_for={[x.id for x in R.open_for(WILL)]}")
case("r a deferred handoff is still returned by stale",
     [x.id for x in R.stale(older_than_s=0.0)] == [hr.id],
     f"stale={[x.id for x in R.stale(older_than_s=0.0)]}")
R.record(hr.id, DEFERRED, reason="compression in flight")
case("r a TWICE-deferred handoff is still owed",
     [x.id for x in R.open_for(WILL)] == [hr.id] and
     [x.id for x in R.stale(older_than_s=0.0)] == [hr.id])
case("r non-vacuity: the latest row really is deferred",
     R.get(hr.id).status == DEFERRED)
R.record(hr.id, DELIVERED)
case("r delivery ends it: gone from open_for AND from stale",
     R.open_for(WILL) == [] and R.stale(older_than_s=0.0) == [])
# and a failed handoff is closed, not owed forever
hrf = R.open_handoff(from_session=BRITTA, to_session=WILL,
                     requesting_user="Britta", intent="will fail")
R.record(hrf.id, FAILED, reason="peer unreachable")
case("r a failed handoff is not owed", R.open_for(WILL) == [])

# --- s: delivered_ts is a DELIVERY time, not a transition time -------------
#     Case f only asserted it is set on delivery. Stamping it on every
#     transition passed all 36 cases -- and a deferred row carrying a delivery
#     timestamp is a false record of arrival in the one field that answers
#     "did this reach anyone".
sp = os.path.join(tmp, "delivered-ts.jsonl")
T = HandoffStore(sp, author="wren")
hs = T.open_handoff(from_session=BRITTA, to_session=WILL,
                    requesting_user="Britta", intent="ts subject")
case("s an open handoff has no delivered_ts", hs.delivered_ts is None)
dfr = T.record(hs.id, DEFERRED, reason="leased")
case("s a DEFERRED row carries no delivered_ts", dfr.delivered_ts is None,
     f"delivered_ts={dfr.delivered_ts}")
dl = T.record(hs.id, DELIVERED)
case("s only the DELIVERED row carries a delivered_ts",
     dl.delivered_ts is not None)
hs2 = T.open_handoff(from_session=BRITTA, to_session=WILL,
                     requesting_user="Britta", intent="fail subject")
fl = T.record(hs2.id, FAILED, reason="peer unreachable")
case("s a FAILED row carries no delivered_ts", fl.delivered_ts is None,
     f"delivered_ts={fl.delivered_ts}")

# --- t: the two refusal guards are separately observable -------------------
#     _TERMINAL and _ALLOWED both refuse delivered->deferred, so deleting
#     EITHER left case g passing on the other. Two guards that mask each other
#     are each individually deletable and nobody sees it. Their only
#     distinguishing observable is the message, so the arm reads the message.
tp = os.path.join(tmp, "guards.jsonl")
G = HandoffStore(tp, author="wren")
hg = G.open_handoff(from_session=BRITTA, to_session=WILL,
                    requesting_user="Britta", intent="guard subject")
G.record(hg.id, DELIVERED)
try:
    G.record(hg.id, DEFERRED, reason="late")
    case("t terminal refusal says TERMINAL, not just 'illegal'", False, "accepted")
except HandoffStatusError as exc:
    case("t terminal refusal says TERMINAL, not just 'illegal'",
         "terminal" in str(exc).lower() and "delivered" in str(exc).lower(),
         f"msg={exc}")
hgf = G.open_handoff(from_session=BRITTA, to_session=WILL,
                     requesting_user="Britta", intent="failed guard subject")
G.record(hgf.id, FAILED, reason="peer unreachable")
try:
    G.record(hgf.id, DELIVERED)
    case("t a FAILED handoff is refused AS TERMINAL", False, "accepted")
except HandoffStatusError as exc:
    # Narrowing _TERMINAL to {delivered} leaves the VERDICT unchanged, because
    # _ALLOWED has no `failed` key and refuses anyway. The two guards mask each
    # other on the verdict, so the message is the only place the distinction
    # survives -- and the distinction matters: "already failed, terminal states
    # do not transition" tells an operator the handoff is closed, while
    # "illegal transition" invites a retry that will never be allowed.
    case("t a FAILED handoff is refused AS TERMINAL",
         "terminal" in str(exc).lower() and "failed" in str(exc).lower(),
         f"msg={exc}")

# a non-terminal, non-allowed transition: reached only via _ALLOWED
hg2 = G.open_handoff(from_session=BRITTA, to_session=WILL,
                     requesting_user="Britta", intent="allowed-table subject")
try:
    G.record(hg2.id, "open")  # type: ignore[arg-type]
    case("t transitioning back to open is refused", False, "accepted")
except HandoffStatusError as exc:
    case("t transitioning back to open is refused",
         "open" in str(exc).lower(), f"msg={exc}")

# --- u: a status transition must not drop the row's parked fields ----------
#     The forward-compat guarantee in from_json() is only worth something if it
#     SURVIVES a transition. Dropping extra{} in record() passed all 36 cases,
#     so a newer writer's fields vanished the moment an older reader deferred.
up = os.path.join(tmp, "extra-survives.jsonl")
U = HandoffStore(up, author="wren")
with open(up, "w") as fh:
    fh.write(json.dumps({
        "id": "carrier", "ts": time.time(), "from_session": BRITTA,
        "to_session": WILL, "requesting_user": "Britta", "intent": "carry me",
        "status": OPEN, "invented_by_a_newer_writer": 42,
    }) + "\n")
case("u non-vacuity: the parked field is present before the transition",
     U.get("carrier").extra.get("invented_by_a_newer_writer") == 42)
moved = U.record("carrier", DEFERRED, reason="leased")
case("u a parked field survives a status transition",
     moved.extra.get("invented_by_a_newer_writer") == 42,
     f"extra={moved.extra}")
case("u it survives on DISK too, not just in the returned object",
     U.get("carrier").extra.get("invented_by_a_newer_writer") == 42)

# --- v: provenance is recorded on every row --------------------------------
#     `author` is the only field saying WHICH agent wrote a row, and both
#     agents write to this log. Nothing asserted it, on any row.
case("v author is stamped on the opening row", U.get("carrier") is not None and
     moved.author == "wren", f"author={moved.author}")
vp = os.path.join(tmp, "author.jsonl")
V = HandoffStore(vp, author="ash")
hv = V.open_handoff(from_session=WILL, to_session=BRITTA,
                    requesting_user="Will", intent="author subject")
case("v open_handoff records the store's author", hv.author == "ash",
     f"author={hv.author}")
case("v the author reaches disk", '"author": "ash"' in Path(vp).read_text())
vt = V.record(hv.id, DEFERRED, reason="leased")
case("v a transition records the author too", vt.author == "ash",
     f"author={vt.author}")

# --- w: a corrupt line must not raise out of the ordinary readers ----------
#     _rows() catching a NARROWER exception than the corruption raises turned
#     every reader into a crash and the suite died mid-run rather than
#     reporting a failure -- an aborted run is not a red suite, and it is not a
#     green one either. These arms name the contract: readers absorb, stale()
#     refuses, and it refuses with the module's OWN error type.
wp = os.path.join(tmp, "corrupt-readers.jsonl")
W = HandoffStore(wp, author="wren")
hw = W.open_handoff(from_session=BRITTA, to_session=WILL,
                    requesting_user="Britta", intent="reader subject")
with open(wp, "a") as fh:
    fh.write("{not json at all\n")
for reader_name, reader in (("all_latest", lambda: W.all_latest()),
                            ("open_for", lambda: W.open_for(WILL)),
                            ("get", lambda: W.get(hw.id)),
                            ("damage_report", lambda: W.damage_report())):
    try:
        reader()
        case(f"w {reader_name} absorbs a corrupt line", True)
    except Exception as exc:  # noqa: BLE001
        case(f"w {reader_name} absorbs a corrupt line", False,
             f"{type(exc).__name__}: {exc}")
try:
    W.stale(older_than_s=0.0)
    case("w stale refuses with HandoffStoreDamaged", False, "returned")
except HandoffStoreDamaged:
    case("w stale refuses with HandoffStoreDamaged", True)
except Exception as exc:  # noqa: BLE001
    case("w stale refuses with HandoffStoreDamaged", False,
         f"{type(exc).__name__}: {exc}")

# --- x: THE FULL TRANSITION MATRIX -----------------------------------------
#     Deleting the `_ALLOWED` check outright changed NOTHING observable: given
#     the status whitelist at the top of record() and the `_TERMINAL` check
#     above it, `_ALLOWED` as currently written permits every pair that can
#     still reach it. It is not under-tested, it is unreachable -- which means
#     the moment someone narrows the table (say, to stop a deferred handoff
#     being delivered) the narrowing has no effect and no case notices.
#     So drive every (current, target) pair through the real API and pin the
#     whole matrix. Now the table is bound by behaviour, whichever guard
#     enforces it.
_STATES = (OPEN, DEFERRED, DELIVERED, FAILED)
_EXPECTED_OK = {
    (OPEN, DELIVERED), (OPEN, DEFERRED), (OPEN, FAILED),
    (DEFERRED, DELIVERED), (DEFERRED, DEFERRED), (DEFERRED, FAILED),
}


def _store_at(state, tag):
    """A fresh store whose only handoff is in ``state``."""
    path = os.path.join(tmp, f"matrix-{tag}.jsonl")
    st = HandoffStore(path, author="wren")
    hh = st.open_handoff(from_session=BRITTA, to_session=WILL,
                         requesting_user="Britta", intent=f"matrix {tag}")
    if state != OPEN:
        st.record(hh.id, state, reason=None if state == DELIVERED else "setup")
    assert st.get(hh.id).status == state, f"setup failed for {state}"
    return st, hh


_matrix_ok, _matrix_bad = set(), []
for _ci, _cur in enumerate(_STATES):
    for _ti, _tgt in enumerate(_STATES):
        _st, _hh = _store_at(_cur, f"{_ci}-{_ti}")
        try:
            _st.record(_hh.id, _tgt, reason=None if _tgt == DELIVERED else "matrix")
            _matrix_ok.add((_cur, _tgt))
        except HandoffStatusError:
            pass
        except Exception as _exc:  # noqa: BLE001
            _matrix_bad.append((_cur, _tgt, type(_exc).__name__))

case("x every legal transition is accepted and every illegal one refused",
     _matrix_ok == _EXPECTED_OK,
     f"unexpectedly allowed={sorted(_matrix_ok - _EXPECTED_OK)} "
     f"wrongly refused={sorted(_EXPECTED_OK - _matrix_ok)}")
case("x every refusal used HandoffStatusError, not an incidental exception",
     _matrix_bad == [], f"{_matrix_bad}")
case("x non-vacuity: the matrix covered all 16 pairs",
     len(_STATES) ** 2 == 16 and len(_matrix_ok) == 6, f"allowed={len(_matrix_ok)}")

# --- q: the suite touched no production path -------------------------------
#     The original form asserted `not prod.exists()`. That is a claim about the
#     WHOLE MACHINE, not about this suite: on any box where handoffs are
#     actually used the production store exists, and the case fails forever
#     while nothing is wrong. It was red on ha-pi from the first real handoff.
#     The claim the suite can actually make is that IT did not touch that path,
#     so snapshot the file before anything is constructed and compare after.
_PROD_AFTER = _prod_state()
case("q suite did not create or modify the production store",
     _PROD_AFTER == _PROD_BEFORE,
     f"before={_PROD_BEFORE} after={_PROD_AFTER}")
case("q non-vacuity: the suite's own store is under a temp dir and was written",
     Path(store_path).exists() and str(Path(store_path).resolve()).startswith(
         str(Path(tempfile.gettempdir()).resolve())))

print()
print(f"  {len(fails)} failed: {fails}" if fails else "  ALL PASS")
sys.exit(1 if fails else 0)
