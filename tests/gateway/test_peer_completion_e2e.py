"""End-to-end: an accepted peer turn is PUBLISHED and then DELIVERED.

WHY THIS SUITE EXISTS SEPARATELY from the requeue suite: that one proves the
drain's routing property on a pure function. This one proves the two halves
are actually CONNECTED — that the producer emits an event the consumer
recognises. A producer and a consumer can each be perfectly correct and never
meet (wrong event type, wrong queue, wrong key names), and every unit test of
either half still passes. That failure is invisible from inside either half.

Both halves are imported from the REAL modules. Nothing about the contract
between them is restated here, because a restated contract tests the
restatement.

Run: python3 tests/gateway/test_peer_completion_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import queue
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
print("E2E: accepted peer turn -> published event -> delivered reply")
print(f"  tree: {TREE}")
print("=" * 70)

run_py = os.path.join(TREE, "gateway", "run.py")
api_py = os.path.join(TREE, "gateway", "platforms", "api_server.py")
check("S1 both subjects exist", os.path.isfile(run_py) and os.path.isfile(api_py),
      "a missing subject is a FAILURE, not a skip")
if not (os.path.isfile(run_py) and os.path.isfile(api_py)):
    print("SUBJECT ABSENT. Verdict withheld.")
    sys.exit(2)

api_src = open(api_py, encoding="utf-8").read()
run_src = open(run_py, encoding="utf-8").read()

# ---- A: THE HALVES MUST AGREE ON THE EVENT TYPE ----------------------
# This is the connection that unit tests of either half cannot see.
check("A1 the producer publishes type 'peer_completion'",
      '"type": "peer_completion"' in api_src,
      "accept path never publishes a peer_completion event — the watcher "
      "would drain a queue nothing writes to")

check("A2 the consumer selects on the SAME literal",
      'evt.get("type") == "peer_completion"' in run_src,
      "drain does not select the type the producer emits")

# ---- B: THE ROW MUST NOT CLOSE ON COMPLETION -------------------------
# The turn finishing is not the reply arriving.
producer_block = api_src[api_src.find("async def _run_and_record"):]
producer_block = producer_block[:3000]
check("B1 the producer does NOT record DELIVERED on turn completion",
      "store.record(handoff.id, DELIVERED)" not in producer_block,
      "closing the row when the turn ends makes an UNDELIVERED reply look "
      "delivered, and stale() goes quiet about the case it exists to report")

check("B2 the watcher is what records DELIVERED",
      "DELIVERED" in run_src and "_deliver_peer_completion" in run_src,
      "nothing closes the row on delivery")

# ---- C: REAL WIRE TEST -----------------------------------------------
from gateway.run import _drain_peer_completions  # noqa: E402

# Build the exact event shape the producer emits, read OUT of the producer
# source rather than retyped, so a key rename breaks this test instead of
# silently passing.
for key in ("session_id", "text", "peer", "handoff_id"):
    check(f"C0 producer event carries '{key}'",
          f'"{key}":' in producer_block,
          f"producer does not set {key}; the consumer reads it")

q = queue.Queue()
foreign = [{"type": "async_delegation", "id": "d1"},
           {"type": "watch_match", "id": "w1"}]
mine = {"type": "peer_completion", "session_id": "s1", "text": "pong",
        "peer": "wren", "handoff_id": "h1"}
for e in foreign:
    q.put(e)
q.put(mine)

taken = _drain_peer_completions(q)
check("C1 the drain takes exactly the producer's event",
      taken == [mine], f"took {taken}")
left = []
while not q.empty():
    left.append(q.get_nowait())
check("C2 both foreign events survive", len(left) == 2, f"left {left}")

# ---- D: DELIVERY CLOSES THE ROW, FAILURE LEAVES IT OPEN --------------
from gateway.handoff import DELIVERED, OPEN, HandoffStore  # noqa: E402

tmp = tempfile.mkdtemp(prefix="wren-e2e-")
path = os.path.join(tmp, "handoffs.jsonl")
store = HandoffStore(path, author="test")
h = store.open_handoff(from_session="wren", to_session="s1",
                       requesting_user="wren", intent="say pong")

import gateway.run as R  # noqa: E402


class FakeRunner:
    _running = True

    def __init__(self, inject_ok):
        self._inject_ok = inject_ok
        self.injected = []

    async def _inject_peer_completion_turn(self, session_id, text, evt):
        self.injected.append((session_id, text))
        return self._inject_ok


FakeRunner._deliver_peer_completion = R.GatewayRunner._deliver_peer_completion

os.environ["HERMES_HOME"] = tmp


async def drive():
    evt = {"type": "peer_completion", "session_id": "s1", "text": "pong",
           "peer": "wren", "handoff_id": h.id}

    good = FakeRunner(inject_ok=True)
    ok = await good._deliver_peer_completion(evt)
    check("D1 successful delivery returns True", ok is True, f"got {ok}")
    check("D2 the reply text actually reached the injector",
          good.injected == [("s1", "pong")], f"{good.injected}")
    rows = HandoffStore(path, author="r").all_latest()
    check("D3 delivery CLOSES the handoff row",
          rows and rows[0].status == DELIVERED,
          f"status={rows[0].status if rows else 'none'}")

    # Failure arm on a fresh row.
    store2 = HandoffStore(path, author="test")
    h2 = store2.open_handoff(from_session="wren", to_session="s2",
                             requesting_user="wren", intent="say pong again")
    bad = FakeRunner(inject_ok=False)
    evt2 = dict(evt, session_id="s2", handoff_id=h2.id)
    ok2 = await bad._deliver_peer_completion(evt2)
    check("D4 failed delivery returns False so the caller requeues",
          ok2 is False, f"got {ok2}")
    rows2 = [r for r in HandoffStore(path, author="r").all_latest() if r.id == h2.id]
    check("D5 FAILED delivery LEAVES the row open (still owed)",
          rows2 and rows2[0].status == OPEN,
          f"status={rows2[0].status if rows2 else 'none'}")
    check("D6 NON-VACUITY: the two arms differ",
          ok is True and ok2 is False)


asyncio.run(drive())

print()
if fails:
    print(f"  {len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print(f"  ALL PASS ({ran} cases)")
