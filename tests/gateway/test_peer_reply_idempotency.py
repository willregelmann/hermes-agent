"""Issue #80, the real fix: a duplicate peer reply must produce ONE turn.

PR #82 kills the orphaned `hermes peer dm` child when the return leg times
out, which NARROWS the duplicate window. It cannot close it. The surviving
case is structural and no timeout can distinguish its two halves:

    the child POSTs the reply -> the receiver accepts and enqueues it
    -> the ack is lost (or the child is killed before reading it)
    -> the sender cannot tell "accepted, ack lost" from "never arrived"
    -> the handoff stays open and is requeued -> the reply is delivered TWICE

The sender is RIGHT to retry: dropping a real reply is the worse failure, and
`_return_peer_completion` deliberately leaves the row open on anything short
of a confirmed delivery. So the fix cannot live on the send side. The receiver
has to recognise that it has already seen this reply.

DESIGN UNDER TEST — deliberately the smallest thing that could work:

  * The SENDER stamps each return-leg dm with a stable id. `handoff_id`
    ALREADY EXISTS on the event (run_peer_completion.py:149, :450) and already
    identifies exactly one logical reply, so this extends a field rather than
    inventing one. It must be stamped ONCE per logical reply, not per attempt,
    or every retry looks new and the key is decorative.
  * It crosses the wire in the same `extra` dict that already carries
    `reply_to` and `from` (peer.py:457-462). Older peers ignore unknown keys.
  * The RECEIVER records ids it has already enqueued and refuses a repeat,
    returning the same accept shape as the first time. A retry must look like
    success to the sender, or the sender keeps retrying forever.

WHAT THESE TESTS PIN, and why each would fail for a different reason:

  1. two deliveries of the same reply id produce exactly ONE enqueued turn
  2. a retry is ACCEPTED, not errored (else the sender never stops)
  3. two DIFFERENT ids both get through (the de-dup is not a global mute)
  4. a reply with NO id still gets through (older senders keep working)

Test 4 is the one that keeps this honest: fail-closed on a missing key would
silently drop every reply from an un-upgraded peer, which is the exact class
of failure the whole return-leg thread has been about.

STATUS: test 1 is RED BY CONSTRUCTION — the de-dup does not exist yet. Tests
2-4 pass against today's behaviour ON PURPOSE: they are the guardrails the
implementation must not break, so they have to be green now and stay green.
A blanket xfail over the whole file would have been wrong, and was: it marked
three already-satisfied properties as "expected to fail" and strict-XPASSed
them, which reads as three broken tests instead of three held invariants.
"""

import pytest


class FakeReceiver:
    """Stand-in for the receive side's enqueue path.

    Deliberately NOT the real api_server: the point of this file today is to
    pin the CONTRACT so the design can be attacked before any storage exists.
    When the real implementation lands, this fake is replaced by the real
    `_enqueue_session_chat` seam and these same four assertions must hold.
    """

    def __init__(self):
        self.enqueued = []

    def post(self, body):
        # The production seam this mirrors:
        #   api_server.py:3202  if body.get("wait") is False:
        #   api_server.py:3203      return await self._enqueue_session_chat(...)
        # No idempotency check exists there today, which is why this is red.
        self.enqueued.append(body)
        return {"accepted": True}


def _reply(idem=None, text="the reply"):
    body = {"wait": False, "message": text, "from": "wren"}
    if idem is not None:
        body["idempotency_key"] = idem
    return body


@pytest.mark.xfail(
    reason="#80: receiver has no idempotency check yet; this is the red test "
           "the implementation must turn green",
    strict=True,
)
def test_duplicate_reply_id_produces_one_turn():
    r = FakeReceiver()
    r.post(_reply("handoff-abc"))
    r.post(_reply("handoff-abc"))
    assert len(r.enqueued) == 1, (
        f"the same reply was enqueued {len(r.enqueued)} times; a killed child "
        "that already delivered plus a requeued retry = a duplicate turn"
    )


def test_duplicate_is_accepted_not_errored():
    r = FakeReceiver()
    r.post(_reply("handoff-abc"))
    second = r.post(_reply("handoff-abc"))
    assert second.get("accepted") is True, (
        "a de-duped retry must look like success, or the sender treats it as "
        "a failure, leaves the handoff open and retries forever"
    )


def test_different_ids_both_get_through():
    r = FakeReceiver()
    r.post(_reply("handoff-abc"))
    r.post(_reply("handoff-def"))
    assert len(r.enqueued) == 2, "de-dup must key on the id, not mute the path"


def test_reply_without_an_id_still_delivers():
    r = FakeReceiver()
    r.post(_reply(None))
    r.post(_reply(None))
    assert len(r.enqueued) == 2, (
        "an un-upgraded sender stamps no key; refusing those would silently "
        "drop real replies, which is worse than the duplicate being fixed"
    )
