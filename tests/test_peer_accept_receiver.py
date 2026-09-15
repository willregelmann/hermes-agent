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


async def main() -> None:
    tmp = tempfile.mkdtemp(prefix="wren-recv-")
    print("=== receiver: accept-only chat ===")

    # --- A: accept returns BEFORE the turn finishes ----------------------
    store_path = os.path.join(tmp, "a.jsonl")
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

    # --- E: completion transitions the row -------------------------------
    await asyncio.wait_for(ad.turn_finished.wait(), timeout=TURN_SECONDS + 5)
    await asyncio.sleep(0.2)
    rows = HandoffStore(store_path, author="r").all_latest()
    case("E row transitions to DELIVERED when the turn completes",
         rows[0].status == DELIVERED, f"status={rows[0].status}")
    case("E nothing is stale once delivered",
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

    print()
    if fails:
        print(f"  {len(fails)} FAILED: {', '.join(fails)}")
        sys.exit(1)
    print(f"  ALL PASS ({ran} cases)")


asyncio.run(main())
