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
S.record(h2.id, DEFERRED, reason="target session leased")
again = S.record(h2.id, DEFERRED, reason="compression in flight")
case("i deferred can defer again (retryable)", again.status == DEFERRED)
final = S.record(h2.id, DELIVERED)
case("i deferred can still be delivered", final.status == DELIVERED)

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
except HandoffStatusError:
    case("m unknown id refused", True)

# --- n: a corrupt line does not hide the rest of the log -------------------
with open(store_path, "a") as fh:
    fh.write("{not json at all\n")
case("n corrupt line skipped, valid rows still read",
     S.get(h3.id) is not None and len(S.all_latest()) == 3)

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

# --- q: the suite touched no production path -------------------------------
prod = Path.home() / ".hermes" / "handoffs.jsonl"
case("q suite did not create a production store", not prod.exists())

print()
print(f"  {len(fails)} failed: {fails}" if fails else "  ALL PASS")
sys.exit(1 if fails else 0)
