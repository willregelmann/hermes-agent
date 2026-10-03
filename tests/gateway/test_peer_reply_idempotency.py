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

DESIGN REVIEW: Wren attacked this shape in handoff 9ab66916 and could not kill
it. She tried the premise first — can the sender distinguish "accepted, ack
lost" from "never arrived"? No, and structurally so: the only evidence is the
ABSENCE of an ack and both cases produce exactly that. Distinguishing them would
require asking the receiver what it has seen, which is this design arriving by
another road. Her four results:

(a) THE KEY IS RIGHT, AND THE INVARIANT IT RESTS ON IS NOW WRITTEN DOWN.
    Verified her structural claim rather than taking it: api_server.py:3317 and
    :3347 both set handoff_id from `handoff.id`, so the id names the INBOUND
    handoff being replied to, NOT the reply. That is fine while the mapping is
    one-reply-per-handoff and nothing enforces that, so it is recorded as a
    named invariant below.
    Her correction to my reasoning, better than mine: the requeue path puts the
    SAME evt dict back on the queue, so handoff_id is already stable across
    attempts. "Per logical reply, not per attempt" needs NO new code.

(b) STORAGE: SHE SAID DON'T DECIDE WITHOUT A MEASUREMENT, AND I TOOK IT.
    Her framing: the failure that matters is not "a restart loses the set", it
    is what the receiver does with the first duplicate AFTER a restart, which is
    deliver it again — one duplicate per restart, roughly today's behaviour.
    Disk does not fix that for free, because eviction reintroduces the same
    window at the horizon. So: can retention be made shorter than the SENDER'S
    RETRY HORIZON?
    MEASURED (/tmp/horizon.py, this tree): THE RETRY HORIZON IS UNBOUNDED.
      * An OPEN handoff is still owed by open_for() at simulated ages of 1s,
        1h, 1 day, 30 days — True in every arm.
      * Negative control: marking it DELIVERED makes open_for() stop owing it,
        so open_for is not merely returning everything.
      * HandoffStore.stale(older_than_s) takes a CALLER-SUPPLIED bound, and an
        AST scan of every non-test .py finds ZERO call sites. Nothing ages a
        handoff out. (grep reported 2 hits; both were docstring prose — the AST
        is the instrument that matches the claim.)
    CONSEQUENCE: a time-bounded in-memory set cannot be sufficient, because no
    finite bound is shorter than an unbounded horizon. Either the seen-set is
    durable with an eviction policy that IS part of the design, or (c) makes the
    question much smaller. Recorded so the choice is evidence-led, not guessed.

(c) PROMOTED FROM OPTION TO REQUIREMENT — her call, and she is right.
    Accept-silently leaves the sender retrying forever against a receiver that
    is successfully ignoring it: the row never closes, the retry never stops,
    and NOTHING records that the reply landed. That is the original bug in a
    quieter coat. So the response MUST distinguish accepted-new from
    accepted-duplicate, and the sender MUST treat accepted-duplicate as success.
    What that buys is the whole point: the retry after a lost ack returns
    accepted-duplicate, the sender closes, and the window I called permanently
    open is CLOSED — not by making the sender able to distinguish, but by making
    the SECOND ATTEMPT AUTHORITATIVE. See test 5.

(d) WARN AND DELIVER on a missing key, with her wording caveat. Her outcome-3b
    precedent cuts toward delivery: 3b was a silent PASS where the box HAD
    knowledge and the sender withheld the field. A missing key is outcome 4 —
    no knowledge to apply — and outcome 4 warns and delivers. The warning fires
    for every reply from every un-upgraded peer, so it must name the peer and
    read as a capability notice, not a fault. A warning that fires constantly
    gets filtered, and a filtered warning is not a warning.

HER ADDITION, WHICH I HAD MISSED: a suppressed duplicate must be OBSERVABLE. If
the receiver suppresses silently and records nothing, nobody can answer "did
this reply arrive twice" a month later — and that question is what started #80.
An unobservable success and a silent failure look identical from outside.
See test 6.
"""

import pytest


class FakeReceiver:
    """Stand-in for the receive side's enqueue path.

    Deliberately NOT the real api_server: the point of this file today is to
    pin the CONTRACT so the design can be attacked before any storage exists.
    When the real implementation lands, this fake is replaced by the real
    `_enqueue_session_chat` seam and these same six assertions must hold.

    NAMED INVARIANT THE KEY RESTS ON (Wren 9ab66916 (a); verified at
    api_server.py:3317 and :3347, where handoff_id is set from `handoff.id`):

        ONE LOGICAL REPLY PER handoff_id.

    handoff_id names the INBOUND handoff being replied to, not the reply
    itself. De-dup on it is correct ONLY while that mapping holds. If one
    handoff ever produces two logical replies, this de-dup SILENTLY DROPS THE
    SECOND — a drop, which is the failure class #80 exists to eliminate, and it
    would be introduced by someone adding a legitimate feature rather than by
    carelessness. If you are making that change: give the reply its own id
    first. Do not "simplify" this by removing the invariant comment.
    """

    def __init__(self):
        self.enqueued = []
        self.observed = []     # Wren 9ab66916: a suppressed duplicate must be findable.
        self.warnings = []     # (d): no-key path warns and delivers.

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


@pytest.mark.xfail(
    reason="#80: the response does not distinguish accepted-new from "
           "accepted-duplicate yet; Wren 9ab66916 (c) promoted this from "
           "option to requirement and it is the half that actually CLOSES "
           "the ack-lost window",
    strict=True,
)
def test_duplicate_response_tells_the_sender_it_may_close():
    """The second attempt must be AUTHORITATIVE, not merely tolerated.

    Wren's argument, which is better than my original design: suppressing the
    duplicate while returning a bare "accepted" leaves the sender retrying
    forever against a receiver that is successfully ignoring it. The row never
    closes and nothing records that the reply landed — the original bug in a
    quieter coat.

    With this, the retry after a lost ack returns accepted-duplicate, the
    sender closes the handoff, and the window I called structurally open is
    CLOSED. The sender must treat duplicate=True as SUCCESS, not as an error.
    """
    r = FakeReceiver()
    first = r.post(_reply("handoff-abc"))
    second = r.post(_reply("handoff-abc"))
    assert first.get("duplicate") is False, (
        "the first delivery must say duplicate=False, or the sender cannot "
        f"tell the two apart; got {first!r}"
    )
    assert second.get("duplicate") is True, (
        "a de-duped retry must say so explicitly so the sender can CLOSE the "
        f"handoff instead of retrying forever; got {second!r}"
    )


@pytest.mark.xfail(
    reason="#80: a suppressed duplicate currently leaves no record; "
           "Wren 9ab66916 added this one and I had missed it",
    strict=True,
)
def test_a_suppressed_duplicate_is_observable_afterwards():
    """Correct and unauditable is not good enough.

    Wren's addition: if the receiver suppresses silently and records nothing,
    nobody can answer "did this reply arrive twice" a month later — and that
    question is exactly what started #80. An unobservable success and a silent
    failure look identical from outside, which is the lesson of this whole
    thread. The record must name the id, or it cannot be tied to an incident.
    """
    r = FakeReceiver()
    r.post(_reply("handoff-abc"))
    r.post(_reply("handoff-abc"))
    assert len(r.observed) == 1, (
        f"expected exactly one recorded suppression, got {len(r.observed)}: "
        "a duplicate that is dropped without a trace cannot be investigated"
    )
    assert "handoff-abc" in str(r.observed[0]), (
        f"the record must name the id to be tied to an incident; got {r.observed[0]!r}"
    )
