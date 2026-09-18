"""Receiver half: POST /chat with wait=false enqueues and answers immediately.

WHY A REAL SERVER, NOT MOCKS: the thing under test is what comes back OVER THE
WIRE and how long it takes. A mocked handler could assert we call the function
we told it to call; it cannot observe that the response carried no assistant
content, or that it arrived before the turn finished. Both are the contract.

THE CONTRACT (PR #17, learned the hard way): an accept response MUST NOT carry
assistant content. The sender decides "completed vs enqueued" from the SHAPE of
the response, not from a version probe — because my merged #12 printed
"accepted, reply will arrive as an inbound DM" over the top of a reply the peer
had already returned, discarding a finished answer. Content present means
finished. Absent means genuinely enqueued.

Run: python3 tests/test_peer_accept_receiver.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails: list[str] = []
ran = 0


def case(name: str, ok: bool, detail: str = "") -> None:
    global ran
    ran += 1
    if ok:
        print(f"  PASS  {name}")
    else:
        fails.append(name)
        print(f"  FAIL  {name}  {detail}")


try:
    from aiohttp import web
except Exception as exc:  # noqa: BLE001
    print(f"  SKIP — aiohttp unavailable: {exc}")
    sys.exit(0)

from gateway.handoff import DELIVERED, OPEN, HandoffStore  # noqa: E402

TURN_SECONDS = 2.0


class FakeAdapter:
    """The real accept logic, with only _run_agent and the store faked.

    _enqueue_session_chat is imported from the production class rather than
    reimplemented — a copy would test the copy.
    """

    def __init__(self, store_path, *, turn_raises=False):
        self._accepted_chat_tasks: set = set()
        self._store = HandoffStore(store_path, author="test")
        self._turn_raises = turn_raises
        self.turn_started = asyncio.Event()
        self.turn_finished = asyncio.Event()

    def _handoff_store(self):
        return self._store

    async def _conversation_history_for_session(self, session_id):
        return []

    async def _run_agent(self, **kw):
        self.turn_started.set()
        await asyncio.sleep(TURN_SECONDS)
        if self._turn_raises:
            raise RuntimeError("model call failed")
        self.turn_finished.set()
        return ({"final_response": "done thinking", "session_id": kw["session_id"]}, {})


# Bind the REAL method onto the fake. The class is APIServerAdapter, not
# APIServerPlatform — I guessed the name and the import failed loudly, which
# is the good version of that mistake.
from gateway.platforms.api_server import APIServerAdapter  # noqa: E402

FakeAdapter._enqueue_session_chat = APIServerAdapter._enqueue_session_chat



# ---------------------------------------------------------------------------
# I / J / K / L: added 2026-09-17 after a mutation audit of merged PR #21.
# 17 mutants against the accept-only receiver; the six original cases killed
# six. Eight of the eleven survivors were real holes (two more, M2 and M10/M11,
# are killed by the suites on open PRs #44 and #45 — cross-checked, not
# assumed). Each case below names the mutant it exists to kill.
# ---------------------------------------------------------------------------

async def drive_gate():
    """I: the wait=false GATE, on the real entry point.

    Every case above calls _enqueue_session_chat DIRECTLY, so the line that
    DECIDES to enqueue had no arm at all. M1 widened `body.get("wait") is
    False` to `is not True` and passed all 19: an ordinary synchronous chat,
    which sends no wait key, then returns 202 with no assistant content and
    the caller reads a finished turn as enqueued — the #17 contract violated
    from the other side.

    The handler is wrapped by _admit_api_agent_request. Satisfy the gate
    rather than reaching past it via __wrapped__: the point of driving the
    entry point is that nothing upstream returns first.
    """
    import types
    import gateway.platforms.api_server as A

    async def once(body_extra):
        cap = {"enqueued": False, "ran_sync": False}

        class FakeAdapter:
            _model_name = "virtual"
            _pending_agent_requests = 0

            def _room_grant_token(self, request): return None
            def _check_auth(self, request): return None
            def _draining_response(self): return None
            def _parse_session_key_header(self, request): return ("k", None)
            async def _get_existing_session_or_404(self, sid): return ({"id": sid}, None)
            async def _read_json_body(self, request):
                return (dict({"message": "hi"}, **body_extra), None)
            def _effective_session_runtime_request(self, session=None, body=None): return {}
            def _runtime_lock_error(self, rr): return None
            def _persist_session_runtime_lock(self, sid, rr): return True
            def _stored_session_model(self, session): return None
            def _resolve_route(self, alias): return None
            def _request_route_conflict_error(self, **kw): return None

            async def _enqueue_session_chat(self, **kw):
                cap["enqueued"] = True
                cap.update(kw)
                return "ACCEPTED"

            async def _conversation_history_for_session(self, sid): return []

            async def _run_agent(self, **kw):
                cap["ran_sync"] = True
                return ({"final_response": "sync answer", "session_id": "s-local"}, {})

        FakeAdapter._handle_session_chat = A.APIServerAdapter._handle_session_chat
        req = types.SimpleNamespace(match_info={"session_id": "s-local"},
                                    path="/api/sessions/s-local/chat", headers={})
        try:
            out = await FakeAdapter()._handle_session_chat(req)
        except Exception as exc:  # a sync turn needs more of the adapter than
            out = f"<raised {type(exc).__name__}>"  # we fake; the flags decide
        return out, cap

    _, c_false = await once({"wait": False})
    case("I1 wait=false takes the accept path", c_false["enqueued"] is True,
         f"cap={ {k: c_false[k] for k in ('enqueued', 'ran_sync')} }")
    _, c_absent = await once({})
    case("I2 NO wait key is a SYNCHRONOUS turn, not an accept (kills M1)",
         c_absent["enqueued"] is False,
         "a widened gate turns every ordinary chat into a 202 with no content, "
         "so the sender reads a finished turn as merely enqueued (#17 contract)")
    _, c_true = await once({"wait": True})
    case("I3 wait=true is synchronous too (kills M1)",
         c_true["enqueued"] is False, f"enqueued={c_true['enqueued']}")
    case("I4 NON-VACUITY: the synchronous branch was actually reached",
         c_absent["ran_sync"] is True or c_true["ran_sync"] is True,
         "if neither sync call reached _run_agent, I2/I3 would pass for a "
         "handler that returned early for an unrelated reason")


async def drive_store(tmpdir):
    """J: the REAL _handoff_store. Every case above fakes it wholesale, so the
    method that resolves the record's PATH was untested surface (R13b).

    M14 changed "handoffs.jsonl" to "handoff.jsonl" and passed all 19: the
    receiver would write every row to a file no other reader of the record
    ever opens, and staleness — the whole replacement for the blocking POST —
    goes permanently quiet while looking healthy.
    """
    import gateway.platforms.api_server as A

    home = os.path.join(tmpdir, "store-home")
    os.makedirs(home, exist_ok=True)
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = home
    try:
        adapter = object.__new__(A.APIServerAdapter)
        store = A.APIServerAdapter._handoff_store(adapter)
        case("J1 the real store resolves (non-vacuity for J2/J3)",
             store is not None, "store came back None")
        if store is None:
            return
        h = store.open_handoff(from_session="wren", to_session="s-j",
                               requesting_user="wren", intent="probe")
        canonical = os.path.join(home, "handoffs.jsonl")
        reader = HandoffStore(canonical, author="independent")
        ids = [r.id for r in reader.all_latest()]
        case("J2 rows land where every OTHER reader looks (kills M14)",
             h.id in ids,
             f"an independent reader of {canonical} saw {ids}; a receiver "
             f"writing to a private path leaves staleness permanently silent")
        second = A.APIServerAdapter._handoff_store(adapter)
        case("J3 the store is memoised, not rebuilt per turn (kills M15)",
             second is store, "a fresh store per accept re-reads the whole "
                              "record on every peer turn")
    finally:
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home


async def drive_degraded(tmpdir):
    """K: the two ways the record can be unusable.

    The docstring on _handoff_store says None is "a degraded mode, never a
    silent one", and the except-clause around open_handoff says "a record
    failure must not swallow the work". Both are prose invariants with no arm
    (lesson 73). M16 (`if store is not None` -> `if True`) and M17 (re-raise
    instead of log) each passed all 19 cases, and each turns a missing ROW
    into a lost TURN.
    """
    class NoStoreAdapter(FakeAdapter):
        def _handoff_store(self):
            return None

    class RaisingStore:
        def open_handoff(self, **kw):
            raise OSError("record is read-only")

    class BadStoreAdapter(FakeAdapter):
        def _handoff_store(self):
            return RaisingStore()

    ad_none = NoStoreAdapter(os.path.join(tmpdir, "unused-none.jsonl"))
    resp = await ad_none._enqueue_session_chat(
        session_id="sess-k1", user_message="x", system_prompt=None,
        gateway_session_key="k", route=None, session_model=None,
        runtime_request={}, lock_active=False, agent_overrides={},
        requesting_user="wren")
    body_k = json.loads(resp.body.decode())
    case("K1 no store at all still ACCEPTS the turn (kills M16)",
         resp.status == 202 and body_k.get("accepted") is True,
         f"status={resp.status} body={body_k}")
    case("K1b and the response omits handoff_id rather than inventing one",
         "handoff_id" not in body_k, f"body={body_k}")
    await asyncio.wait_for(ad_none.turn_finished.wait(), timeout=TURN_SECONDS + 5)
    case("K1c the UNTRACKED turn still runs to completion",
         ad_none.turn_finished.is_set(), "a degraded record silently ate the work")

    # The call is GUARDED. A mutant that re-raises past the except clause
    # would otherwise kill the whole run, and an aborted run reads as zero
    # failures to any harness that counts FAIL lines — neither red nor green
    # (lesson 77). A broken subject must produce a NAMED FAILING CASE.
    ad_bad = BadStoreAdapter(os.path.join(tmpdir, "unused-bad.jsonl"))
    resp2 = None
    raised = None
    try:
        resp2 = await ad_bad._enqueue_session_chat(
            session_id="sess-k2", user_message="x", system_prompt=None,
            gateway_session_key="k", route=None, session_model=None,
            runtime_request={}, lock_active=False, agent_overrides={},
            requesting_user="wren")
    except Exception as exc:  # noqa: BLE001
        raised = exc
    case("K2 a store that RAISES does not swallow the accept (kills M17)",
         raised is None and resp2 is not None and resp2.status == 202,
         f"raised={raised!r} — 'a record failure must not swallow the work' "
         f"is stated in the source and was asserted nowhere")
    if raised is None:
        try:
            await asyncio.wait_for(ad_bad.turn_finished.wait(),
                                   timeout=TURN_SECONDS + 5)
        except asyncio.TimeoutError:
            pass
        case("K2b and the turn itself still runs (kills M17)",
             ad_bad.turn_finished.is_set(),
             "re-raising past the except turns an unwritable row into a lost turn")
    else:
        case("K2b and the turn itself still runs (kills M17)", False,
             "the accept raised, so no turn was ever started")

    # K3: M16 is VERDICT-EQUIVALENT and needs the log, not the return value.
    # Changing `if store is not None:` to `if True:` calls open_handoff on
    # None; the AttributeError lands in the except clause right below, which
    # logs and carries on, so status, body and the turn are all identical.
    # Two mechanisms refusing the same input mask each other (lesson 77) —
    # what distinguishes them is WHAT THE OPERATOR IS TOLD. The None-store
    # path is supposed to be quietly degraded; reaching open_handoff on None
    # reports a handoff FAILURE that never happened, on every single turn.
    import gateway.platforms.api_server as _A
    seen_msgs = []
    real_exc = _A.logger.exception

    def _spy(msg, *a, **kw):
        seen_msgs.append(str(msg))
        return real_exc(msg, *a, **kw)

    _A.logger.exception = _spy
    try:
        ad_none2 = NoStoreAdapter(os.path.join(tmpdir, "unused-none2.jsonl"))
        await ad_none2._enqueue_session_chat(
            session_id="sess-k3", user_message="x", system_prompt=None,
            gateway_session_key="k", route=None, session_model=None,
            runtime_request={}, lock_active=False, agent_overrides={},
            requesting_user="wren")
        await asyncio.wait_for(ad_none2.turn_finished.wait(),
                               timeout=TURN_SECONDS + 5)
        await asyncio.sleep(0.2)
    finally:
        _A.logger.exception = real_exc
    case("K3 a None store is SKIPPED, not called and caught (kills M16)",
         not any("could not open a handoff row" in m for m in seen_msgs),
         f"logged={seen_msgs} — the guard and the except clause refuse the "
         f"same input, so only the message tells them apart")


async def drive_handles(tmpdir):
    """L: the things the CALLER is left holding.

    A mutant that deletes a recovery HANDLE leaves the VERDICT intact, so no
    status/accepted assertion can see it (lesson 80, second half). M13 dropped
    the X-Hermes-Session-Id header and M12 put the LOCAL session id in the
    completion event's `peer` field; both passed all 19.
    """
    from tools.process_registry import process_registry as _pr

    while True:
        try:
            _pr.completion_queue.get_nowait()
        except Exception:
            break

    ad = FakeAdapter(os.path.join(tmpdir, "l.jsonl"))
    resp = await ad._enqueue_session_chat(
        session_id="sess-l", user_message="x", system_prompt=None,
        gateway_session_key="kk", route=None, session_model=None,
        runtime_request={}, lock_active=False, agent_overrides={},
        requesting_user="ash")
    case("L1 the accept names the session in a HEADER too (kills M13)",
         resp.headers.get("X-Hermes-Session-Id") == "sess-l",
         f"headers={dict(resp.headers)} — the session id is the only handle "
         f"to an answer that has not arrived yet")
    case("L1b and echoes the session key (non-vacuity for L1)",
         resp.headers.get("X-Hermes-Session-Key") == "kk",
         f"headers={dict(resp.headers)}")

    await asyncio.wait_for(ad.turn_finished.wait(), timeout=TURN_SECONDS + 5)
    await asyncio.sleep(0.3)
    evts = []
    while True:
        try:
            evts.append(_pr.completion_queue.get_nowait())
        except Exception:
            break
    mine = [e for e in evts if e.get("session_id") == "sess-l"]
    case("L2 the completion event names the REQUESTER as peer (kills M12)",
         bool(mine) and mine[0].get("peer") == "ash",
         f"evt={mine[0] if mine else None} — the local session id here sends "
         f"the reply back to ourselves, the 2026-09-16 live failure")
    case("L3 a finished task is discarded from the strong-ref set (kills M7)",
         len(ad._accepted_chat_tasks) == 0,
         f"n={len(ad._accepted_chat_tasks)} — G proves the task is HELD; "
         f"nothing proved it is ever RELEASED, so the set grows per turn")

async def main() -> None:
    tmp = tempfile.mkdtemp(prefix="wren-recv-")
    print("=== receiver: accept-only chat ===")

    # --- A: accept returns BEFORE the turn finishes ----------------------
    os.environ["HERMES_HOME"] = tmp
    store_path = os.path.join(tmp, "handoffs.jsonl")
    ad = FakeAdapter(store_path)
    t0 = time.time()
    resp = await ad._enqueue_session_chat(
        session_id="sess-1", user_message="do the thing", system_prompt=None,
        gateway_session_key="k", route=None, session_model=None,
        runtime_request={}, lock_active=False, agent_overrides={},
        requesting_user="wren",
    )
    elapsed = time.time() - t0
    body = json.loads(resp.body.decode())

    case("A accept returns before the turn completes",
         elapsed < TURN_SECONDS / 2, f"elapsed={elapsed:.2f}s")
    case("A status is 202 Accepted", resp.status == 202, f"status={resp.status}")

    # --- B: THE CONTRACT — no assistant content --------------------------
    case("B response carries NO assistant content (the #17 contract)",
         "message" not in body and "content" not in json.dumps(body),
         f"body={body}")
    case("B response says accepted and names the session",
         body.get("accepted") is True and body.get("session_id") == "sess-1",
         f"body={body}")

    # --- C: an OPEN row exists while the turn runs -----------------------
    await ad.turn_started.wait()
    rows = HandoffStore(store_path, author="r").all_latest()
    case("C an OPEN handoff row exists during the turn",
         len(rows) == 1 and rows[0].status == OPEN, f"rows={rows}")
    case("C the accept response names the row so it is followable",
         body.get("handoff_id") == rows[0].id, f"id={body.get('handoff_id')}")

    # --- D: staleness — the guarantee the blocking POST used to give -----
    stale = HandoffStore(store_path, author="r").stale(older_than_s=0.0)
    case("D an accepted-but-unfinished turn is visible as stale",
         len(stale) == 1, f"stale={stale}")

    # --- E: the turn finishing is NOT the reply arriving ------------------
    #     The row deliberately stays OPEN when the turn completes (commit
    #     9301898b70): closing on completion would make an undelivered reply
    #     look delivered. Completion PUBLISHES a peer_completion event; the
    #     watcher closes the row after delivery actually happens.
    await asyncio.wait_for(ad.turn_finished.wait(), timeout=TURN_SECONDS + 5)
    await asyncio.sleep(0.2)
    rows = HandoffStore(store_path, author="r").all_latest()
    case("E a completed-but-undelivered turn leaves the row OPEN",
         rows[0].status == OPEN, f"status={rows[0].status}")
    case("E an undelivered reply is still visible as stale",
         len(HandoffStore(store_path, author="r").stale(older_than_s=0.0)) == 1,
         "completion silently cleared staleness")

    from tools.process_registry import process_registry as _pr
    published = []
    while True:
        try:
            published.append(_pr.completion_queue.get_nowait())
        except Exception:
            break
    mine = [e for e in published if e.get("session_id") == "sess-1"]
    case("E completion publishes exactly one peer_completion event",
         len(mine) == 1 and mine[0].get("type") == "peer_completion",
         f"published={published}")
    if mine:
        case("E the event carries the turn text",
             mine[0].get("text") == "done thinking", f"evt={mine[0]}")
        case("E the event names the row so delivery can close it",
             mine[0].get("handoff_id") == rows[0].id, f"evt={mine[0]}")

        # THE TETHER: drive the REAL closer, not a reimplementation of it.
        from gateway.run import GatewayRunner
        GatewayRunner._close_peer_handoff(object.__new__(GatewayRunner), mine[0])
        rows = HandoffStore(store_path, author="r").all_latest()
        case("E delivery (real _close_peer_handoff) closes the row",
             rows[0].status == DELIVERED, f"status={rows[0].status}")
        case("E nothing is stale once actually delivered",
             HandoffStore(store_path, author="r").stale(older_than_s=0.0) == [],
             "still stale after delivery")

    # --- F: a FAILING turn must not leave the row at open ----------------
    #     Ash's finding on the wake primitive, same shape: an exception is a
    #     path nobody wrote, which is where rows go to die.
    store_f = os.path.join(tmp, "f.jsonl")
    adf = FakeAdapter(store_f, turn_raises=True)
    await adf._enqueue_session_chat(
        session_id="sess-2", user_message="explode", system_prompt=None,
        gateway_session_key="k", route=None, session_model=None,
        runtime_request={}, lock_active=False, agent_overrides={},
        requesting_user="wren",
    )
    await asyncio.sleep(TURN_SECONDS + 1.0)
    rows = HandoffStore(store_f, author="r").all_latest()
    case("F a FAILED turn does not leave the row at open",
         rows[0].status != OPEN, f"status={rows[0].status}")
    case("F the failure reason is recorded, not generic",
         "TURN_FAILED" in (rows[0].reason or ""), f"reason={rows[0].reason}")

    # --- G: the task is strongly referenced ------------------------------
    #     asyncio holds only a weak ref; a collected task drops the turn with
    #     no error anywhere. Non-vacuity: assert the set was actually used.
    ad2 = FakeAdapter(os.path.join(tmp, "g.jsonl"))
    await ad2._enqueue_session_chat(
        session_id="sess-3", user_message="x", system_prompt=None,
        gateway_session_key="k", route=None, session_model=None,
        runtime_request={}, lock_active=False, agent_overrides={},
        requesting_user="wren",
    )
    case("G the in-flight task is strongly referenced",
         len(ad2._accepted_chat_tasks) == 1, f"n={len(ad2._accepted_chat_tasks)}")

    # --- H: MUTANT — accept response that carries content ----------------
    #     Reproduces the #12 regression from the RECEIVER side: if the peer
    #     put a placeholder in message.content, every sender would read the
    #     accept as a finished turn and print a fabricated reply.
    mutant_body = dict(body)
    mutant_body["message"] = {"role": "assistant", "content": "queued!"}

    def sender_reads_as_completed(b):
        msg = b.get("message")
        return bool(isinstance(msg, dict) and (msg.get("content") or "").strip())

    case("H MUTANT with content is misread as a completed turn",
         sender_reads_as_completed(mutant_body) is True)
    case("H the real accept response is NOT misread (mutant killed)",
         sender_reads_as_completed(body) is False, f"body={body}")

    # --- I/J/K/L: added by the PR #21 mutation audit ---------------------
    await drive_gate()
    await drive_store(tmp)
    await drive_degraded(tmp)
    await drive_handles(tmp)

    CASE_FLOOR = 35  # measured AFTER the run, not guessed (lesson 79e)
    if ran < CASE_FLOOR:
        fails.append(f"CASE FLOOR: only {ran} cases ran (expected >= {CASE_FLOOR})")
        print(f"  FAIL  case floor  ran={ran}")

    print()
    if fails:
        print(f"  {len(fails)} FAILED: {', '.join(fails)}")
        sys.exit(1)
    print(f"  ALL PASS ({ran} cases)")


asyncio.run(main())
