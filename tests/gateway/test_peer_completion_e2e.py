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


# ======================================================================
# ROUND 14 ADDITIONS (mutation audit of merged PR #29).
# Ten of thirteen mutants survived the original 25 cases. Everything below
# drives a real entry point; nothing below asserts on source text except E1,
# which says so in its own name.
# ======================================================================

import ast  # noqa: E402

# ---- E: THE SPAWN IS UNCONDITIONAL -----------------------------------
# A2 of the requeue suite reads `_spawn_supervised(self._peer_completion_watcher`
# out of the source. Wrapping that exact line in `if False:` leaves the
# substring in place, and the mutant passed all 25 cases. start() is ~1000
# lines of network setup and cannot be driven here, so this arm is
# STRUCTURAL rather than behavioural: parse the file and require the spawn
# call to be a direct statement of its function body, not nested in any
# conditional. That distinguishes "named" from "reached".
_tree = ast.parse(run_src)
_spawn_sites = []
for _fn in ast.walk(_tree):
    if not isinstance(_fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    for _stmt in _fn.body:  # DIRECT body only — no ast.walk here
        if not isinstance(_stmt, ast.Expr):
            continue
        _c = _stmt.value
        if not isinstance(_c, ast.Call):
            continue
        if getattr(_c.func, "attr", None) != "_spawn_supervised":
            continue
        for _a in _c.args:
            if getattr(_a, "attr", None) == "_peer_completion_watcher":
                _spawn_sites.append(_fn.name)
check("E1 STRUCTURAL: the watcher spawn is an unconditional statement",
      len(_spawn_sites) == 1,
      f"found {_spawn_sites} — the call may be present but nested under a "
      f"conditional, which reads exactly like a wired watcher and never runs")

# ---- F: THE WATCHER LOOP REQUEUES ------------------------------------
# Every earlier case drove _drain_peer_completions (pure) or
# _deliver_peer_completion (directly). The loop that joins them — the one
# that decides whether a FAILED delivery is retried or lost — had no arm,
# and both of its requeue branches were deletable green.
from tools.process_registry import process_registry as _PR  # noqa: E402


class WatchRunner:
    """Real _peer_completion_watcher body, one iteration."""

    def __init__(self, outcome):
        self._outcome = outcome        # True | False | "raise"
        self._iters = 0
        self.seen = []

    @property
    def _running(self):
        return self._iters < 1

    async def _deliver_peer_completion(self, evt):
        self._iters += 1
        self.seen.append(evt)
        if self._outcome == "raise":
            raise RuntimeError("delivery blew up")
        return self._outcome


WatchRunner._peer_completion_watcher = R.GatewayRunner._peer_completion_watcher


async def drive_watcher(outcome):
    # Drain anything already queued so the arm reads only its own event.
    while not _PR.completion_queue.empty():
        _PR.completion_queue.get_nowait()
    evt = {"type": "peer_completion", "session_id": "sW", "text": "t",
           "peer": "wren", "handoff_id": "hW"}
    _PR.completion_queue.put(evt)
    runner = WatchRunner(outcome)
    real_sleep = R.asyncio.sleep

    async def _fast(_s):
        await real_sleep(0)

    R.asyncio.sleep = _fast
    try:
        await runner._peer_completion_watcher(interval=0)
    finally:
        R.asyncio.sleep = real_sleep
    left = []
    while not _PR.completion_queue.empty():
        left.append(_PR.completion_queue.get_nowait())
    return runner, left


async def drive_all():
    r_ok, left_ok = await drive_watcher(True)
    check("F0 NON-VACUITY: the watcher really consumed the event",
          r_ok.seen and r_ok.seen[0]["handoff_id"] == "hW",
          f"seen={r_ok.seen!r}")
    check("F1 a DELIVERED event is not requeued", left_ok == [],
          f"left {left_ok!r}")
    _r_no, left_no = await drive_watcher(False)
    check("F2 a FAILED delivery is REQUEUED (the reply stays owed)",
          [e.get("handoff_id") for e in left_no] == ["hW"],
          f"left {left_no!r} — a dropped event is a silently lost reply, the "
          f"exact shape this feature exists to remove")
    _r_ex, left_ex = await drive_watcher("raise")
    check("F3 a RAISING delivery is REQUEUED too",
          [e.get("handoff_id") for e in left_ex] == ["hW"],
          f"left {left_ex!r}")

    # ---- G: THE UNROUTABLE EVENT -------------------------------------
    # The guard that drops an event with no session_id/text was deletable
    # green, and so was the direction of its return value. Returning False
    # there makes the watcher requeue an event that can never be routed,
    # which is a hot loop, not a retry.
    g = FakeRunner(inject_ok=True)
    g_ok = await g._deliver_peer_completion(
        {"type": "peer_completion", "session_id": "", "text": "x"})
    check("G1 an event with no session_id never reaches the injector",
          g.injected == [], f"injected {g.injected!r}")
    check("G2 and is reported TRUE so the watcher drops it rather than spinning",
          g_ok is True, f"got {g_ok!r}")
    g2 = FakeRunner(inject_ok=True)
    g2_ok = await g2._deliver_peer_completion(
        {"type": "peer_completion", "session_id": "s9", "text": "   "})
    check("G3 whitespace-only text is unroutable the same way",
          g2.injected == [] and g2_ok is True,
          f"injected={g2.injected!r} ok={g2_ok!r}")

    # ---- H: THE MIRROR CALL ------------------------------------------
    # _inject_peer_completion_turn had no arm at all: role="assistant" and
    # ignoring the event's chat_id both passed 25 cases. The role one breaks
    # strict-alternation providers, which is stated in the PR body.
    import gateway.mirror as _M
    seen_kw = {}

    def _fake_mirror(**kw):
        seen_kw.update(kw)
        return True

    real_mirror = _M.mirror_to_session
    _M.mirror_to_session = _fake_mirror
    try:
        class InjRunner:
            pass
        InjRunner._inject_peer_completion_turn = \
            R.GatewayRunner._inject_peer_completion_turn
        ok = await InjRunner()._inject_peer_completion_turn(
            "s-inj", "hello",
            {"peer": "wren", "platform": "google_chat", "chat_id": "spaces/AAA"})
    finally:
        _M.mirror_to_session = real_mirror
    check("H0 NON-VACUITY: the real mirror primitive was called",
          ok is True and seen_kw.get("message_text") == "hello",
          f"kw={sorted(seen_kw)}")
    check("H1 the peer's words are mirrored with role='user'",
          seen_kw.get("role") == "user",
          f"role={seen_kw.get('role')!r} — an assistant-role mirror replays as "
          f"this agent speaking and produces assistant->assistant pairs")
    check("H2 the event's chat_id is honoured, not overwritten by session_id",
          seen_kw.get("chat_id") == "spaces/AAA",
          f"chat_id={seen_kw.get('chat_id')!r} — delivery would go to the "
          f"wrong channel")
    check("H3 and the session_id still routes the append",
          seen_kw.get("session_id") == "s-inj", f"{seen_kw.get('session_id')!r}")

    # ---- I: THE PRODUCER, driven on the real accept path --------------
    # A1/C0 read the publish dict out of api_server.py as text. Feeding any
    # of its values None leaves the key in the file, and M10/M11/M12 all
    # passed. Drive _enqueue_session_chat for real and read the QUEUE.
    import gateway.platforms.api_server as A

    async def drive_producer(final_text):
        while not _PR.completion_queue.empty():
            _PR.completion_queue.get_nowait()
        store_p = HandoffStore(path, author="test")

        class FakeAPI:
            _accepted_chat_tasks = set()

            def _handoff_store(self):
                return store_p

            async def _conversation_history_for_session(self, sid):
                return []

            async def _run_agent(self, **kw):
                return ({"final_response": final_text}, {})

        FakeAPI._enqueue_session_chat = A.APIServerAdapter._enqueue_session_chat
        api = FakeAPI()
        resp = await api._enqueue_session_chat(
            session_id="s-prod", user_message="ping", system_prompt=None,
            gateway_session_key="k", route=None, session_model=None,
            runtime_request={}, lock_active=False, agent_overrides={},
            requesting_user="wren", reply_to=None)
        for t in list(FakeAPI._accepted_chat_tasks):
            await t
        events = []
        while not _PR.completion_queue.empty():
            events.append(_PR.completion_queue.get_nowait())
        return resp, events

    resp, events = await drive_producer("pong from the peer")
    check("I0 NON-VACUITY: the accept path answered 202",
          getattr(resp, "status", None) == 202, f"status={getattr(resp,'status',None)}")
    check("I1 exactly one peer_completion event is published",
          len(events) == 1 and events[0].get("type") == "peer_completion",
          f"events={events!r}")
    ev = events[0] if events else {}
    check("I2 the published event carries the REAL session_id",
          ev.get("session_id") == "s-prod",
          f"session_id={ev.get('session_id')!r} — None here makes every reply "
          f"unroutable while '\"session_id\":' stays in the file")
    check("I3 and the REAL handoff id, so the row can be closed",
          isinstance(ev.get("handoff_id"), str) and ev.get("handoff_id"),
          f"handoff_id={ev.get('handoff_id')!r} — None leaves the row open "
          f"forever and stale() reports a reply that was delivered")
    check("I4 the handoff id names a row that actually exists",
          any(r.id == ev.get("handoff_id")
              for r in HandoffStore(path, author="r").all_latest()),
          "the published id matches no row")
    check("I5 the text is the turn's final_response",
          ev.get("text") == "pong from the peer", f"text={ev.get('text')!r}")
    _resp2, events2 = await drive_producer("   ")
    check("I6 a turn with NO text publishes NOTHING",
          events2 == [],
          f"events={events2!r} — an empty event forces the consumer to decide "
          f"about a reply that does not exist")


asyncio.run(drive_all())

# ---- CASE-COUNT FLOOR ------------------------------------------------
# A suite that dies halfway prints no FAIL line, and a fail-counting harness
# reads that as green (lesson 77).
FLOOR = 33
check(f"Z1 CASE FLOOR: at least {FLOOR} cases ran", ran >= FLOOR,
      f"only {ran} ran — a truncated run is not a green run")


print()
if fails:
    print(f"  {len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print(f"  ALL PASS ({ran} cases)")
