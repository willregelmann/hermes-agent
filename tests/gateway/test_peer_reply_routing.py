"""Cross-box delivery: a reply goes back to the SENDER, not to itself.

THE DEFECT THIS ENCODES, measured on a live pair 2026-09-16:
`_deliver_peer_completion` read `session_id` off the completion event. The
producer sets that to the session the TURN RAN IN — a session on the PEER's
box. So the watcher faithfully delivered the reply into the peer's own
transcript. Both halves were internally correct; the reply never crossed the
network. Ash's handoff row 5432a0f3 went open 09:58:01 -> delivered 09:58:26
and my box never saw a thing.

The sender is the only party that knows where the answer should go, so the
sender says. `reply_to` is a hint an old peer ignores, exactly like `wait`.

Run: python3 tests/gateway/test_peer_reply_routing.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

TREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, TREE)

fails: list = []
ran = 0


def check(name, ok, detail=""):
    global ran
    ran += 1
    if ok:
        print(f"  PASS  {name}")
    else:
        fails.append(name)
        print(f"  FAIL  {name}  {detail}")


print("=" * 70)
print("cross-box: a reply returns to the sender")
print(f"  tree: {TREE}")
print("=" * 70)

peer_py = os.path.join(TREE, "hermes_cli", "subcommands", "peer.py")
api_py = os.path.join(TREE, "gateway", "platforms", "api_server.py")
run_py = os.path.join(TREE, "gateway", "run.py")
for p in (peer_py, api_py, run_py):
    if not os.path.isfile(p):
        print(f"SUBJECT ABSENT: {p}")
        sys.exit(2)
check("S1 all three subjects exist", True)

peer_src = open(peer_py, encoding="utf-8").read()
api_src = open(api_py, encoding="utf-8").read()
run_src = open(run_py, encoding="utf-8").read()

# ---- A: the three halves must agree on ONE key name ------------------
check("A1 sender sets reply_to on the --no-wait body",
      'body["reply_to"] = origin' in peer_src,
      "sender declares no return address; --no-wait delivers nowhere")
check("A2 receiver carries reply_to into the completion event",
      '"reply_to": reply_to,' in api_src,
      "accept path drops the return address before publishing")
check("A3 watcher routes on reply_to BEFORE local delivery",
      run_src.find('reply_to = evt.get("reply_to")') != -1
      and run_src.find('reply_to = evt.get("reply_to")')
      < run_src.find("_inject_peer_completion_turn(session_id"),
      "routing decision comes after local injection — reply goes to self")

# ---- B: the sender's return address ----------------------------------
# ORDER IS LOAD-BEARING AND I GOT IT WRONG FIRST. The jailed-home case below
# reloads `hermes_constants` and `peer`; if it runs BEFORE the real-identity
# case, the reloaded modules keep the jail's resolution and B1/B2 fail on a
# perfectly good box. Ask the real question first, then jail.
from hermes_cli.subcommands.peer import _self_origin  # noqa: E402

origin = _self_origin()
check("B1 this box can state its own return address",
      isinstance(origin, dict) and bool(origin.get("agent")),
      f"got {origin!r}")
check("B2 the address is an AGENT NAME, never a session id",
      isinstance(origin, dict) and "session" not in json.dumps(origin).lower(),
      f"{origin!r} — one box must not name a session on another")

# A jailed home with no identity.json must yield None, not a guess. Run this
# in a CHILD interpreter so the reload cannot leak into the cases above or
# below — the leak is exactly what bit me.
import subprocess as _sp  # noqa: E402

jail = tempfile.mkdtemp(prefix="wren-origin-")
_probe = (
    "import os,sys;os.environ['HERMES_HOME']=%r;sys.path.insert(0,%r);"
    "from hermes_cli.subcommands.peer import _self_origin;"
    "print('RESULT=' + repr(_self_origin()))" % (jail, TREE)
)
_r = _sp.run([sys.executable, "-c", _probe], capture_output=True, text=True, timeout=120)
blank_line = [l for l in _r.stdout.splitlines() if l.startswith("RESULT=")]
check("B3 the jailed probe actually ran (non-vacuity)",
      bool(blank_line), f"stdout={_r.stdout[-200:]!r} stderr={_r.stderr[-200:]!r}")
blank = blank_line[0].split("=", 1)[1] if blank_line else "<no result>"
check("B4 no identity -> None, NOT a fabricated address",
      blank == "None",
      f"got {blank}: a wrong return address delivers a real reply nowhere "
      f"while the send still looks successful")

# ---- C: routing, driven on the real method ---------------------------
import gateway.run as R  # noqa: E402
from gateway.handoff import DELIVERED, OPEN, HandoffStore  # noqa: E402

tmp = tempfile.mkdtemp(prefix="wren-route-")
os.environ["HERMES_HOME"] = tmp
path = os.path.join(tmp, "handoffs.jsonl")


class FakeRunner:
    _running = True

    def __init__(self, return_ok=True):
        self.returned = []
        self.injected = []
        self._return_ok = return_ok

    async def _return_peer_completion(self, reply_to, text, evt):
        self.returned.append((reply_to.get("agent"), text))
        return self._return_ok

    async def _inject_peer_completion_turn(self, session_id, text, evt):
        self.injected.append((session_id, text))
        return True


FakeRunner._deliver_peer_completion = R.GatewayRunner._deliver_peer_completion
FakeRunner._close_peer_handoff = R.GatewayRunner._close_peer_handoff


async def drive():
    store = HandoffStore(path, author="test")

    # C: a remote sender -> returns, does NOT inject locally
    h = store.open_handoff(from_session="wren", to_session="s1",
                           requesting_user="wren", intent="x")
    evt = {"type": "peer_completion", "session_id": "ash-local-session",
           "text": "pong", "peer": "wren", "handoff_id": h.id,
           "reply_to": {"agent": "wren", "host": "ha-pi.local"}}
    r = FakeRunner()
    ok = await r._deliver_peer_completion(evt)
    check("C1 a reply_to event RETURNS to the sender", ok is True and
          r.returned == [("wren", "pong")], f"returned={r.returned}")
    check("C2 and does NOT inject into the local session",
          r.injected == [],
          f"injected {r.injected} — this IS the live bug: the reply went "
          f"back into the box that produced it")
    rows = [x for x in HandoffStore(path, author="r").all_latest() if x.id == h.id]
    check("C3 a successful return closes the row",
          rows and rows[0].status == DELIVERED, f"{rows[0].status if rows else None}")

    # D: CONTROL — no reply_to (local/old sender) still injects locally.
    # Without this, C2 passes for a subject that never injects at all.
    h2 = store.open_handoff(from_session="local", to_session="s2",
                            requesting_user="local", intent="y")
    evt2 = {"type": "peer_completion", "session_id": "my-session",
            "text": "local answer", "peer": "self", "handoff_id": h2.id}
    r2 = FakeRunner()
    ok2 = await r2._deliver_peer_completion(evt2)
    check("D1 CONTROL: no reply_to still delivers LOCALLY",
          ok2 is True and r2.injected == [("my-session", "local answer")],
          f"injected={r2.injected}")
    check("D2 CONTROL: and does not try to return",
          r2.returned == [], f"returned={r2.returned}")

    # E: a FAILED return must leave the row open
    h3 = store.open_handoff(from_session="wren", to_session="s3",
                            requesting_user="wren", intent="z")
    evt3 = dict(evt, handoff_id=h3.id)
    r3 = FakeRunner(return_ok=False)
    ok3 = await r3._deliver_peer_completion(evt3)
    check("E1 a failed return reports False so the caller requeues",
          ok3 is False, f"got {ok3}")
    rows3 = [x for x in HandoffStore(path, author="r").all_latest() if x.id == h3.id]
    check("E2 a failed return LEAVES THE ROW OPEN (still owed)",
          rows3 and rows3[0].status == OPEN,
          f"{rows3[0].status if rows3 else None}")
    check("E3 NON-VACUITY: success and failure arms differ",
          ok is True and ok3 is False)


asyncio.run(drive())

print()
if fails:
    print(f"  {len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print(f"  ALL PASS ({ran} cases)")
