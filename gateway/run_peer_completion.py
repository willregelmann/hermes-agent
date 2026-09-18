"""Peer-completion delivery: a finished ``hermes peer dm --no-wait`` turn re-enters its session.

GatewayRunner mixin (fork-only). The api_server accept path publishes a ``peer_completion`` event on
the shared completion queue when a background peer turn finishes; ``_peer_completion_watcher``
drains those events and either returns the reply to the sending agent or injects it into the local
session, closing the handoff row only on delivery.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("gateway.run")


def _drain_peer_completions(completion_queue) -> "list[dict]":
    """Take peer-completion events off the shared queue; requeue everything else.

    THE REQUEUE RULE IS LOAD-BEARING AND ITS VIOLATION IS SILENT. The
    completion queue is multi-consumer with no routing: watch events belong to
    the post-turn gateway drain, process completions to their per-process
    watcher task, and async-delegation events to
    ``_async_delegation_watcher``. A drain that consumes what it does not own
    makes those features "just stop working sometimes", with no error anywhere
    and nothing pointing at this function.

    Detach the current batch FIRST, then requeue what is not ours after the
    queue is empty. Requeueing inside ``while not queue.empty()`` would feed
    the loop its own output and never terminate — the same structure
    ``_drain_gateway_watch_events`` documents in gateway/run.py.

    Pure and synchronous on purpose: the requeue property is the thing most
    likely to be broken by a later edit, and a pure function can be driven by
    a test with a real ``queue.Queue`` and no event loop.
    """
    peer_events: list[dict] = []
    requeue: list[dict] = []
    while not completion_queue.empty():
        try:
            evt = completion_queue.get_nowait()
        except Exception:
            break
        if evt.get("type") == "peer_completion":
            peer_events.append(evt)
        else:
            requeue.append(evt)
    for evt in requeue:
        completion_queue.put(evt)
    return peer_events



class GatewayPeerCompletionMixin:
    """Watcher + delivery for peer_completion events (see module docstring)."""

    async def _peer_completion_watcher(self, interval: float = 2.0) -> None:
        """Deliver finished peer turns into the session that is waiting for them.

        WHAT THIS CLOSES. `hermes peer dm --no-wait` returns the moment the
        peer ACCEPTS (202, no assistant content) and the peer's turn runs in
        the background. Until now the result only PERSISTED — it landed in the
        session and sat there. Nothing delivered it, so the sender had to go
        and look, which is the same "human as transport" gap the accept path
        was built to remove, moved one step later.

        This is the idle case, and it is the whole point: when a peer turn
        finishes while no local turn is running, the reply must still re-enter
        the conversation promptly rather than waiting for the next thing a
        human types.

        Modelled on ``_async_delegation_watcher`` deliberately, including its
        requeue discipline — see ``_drain_peer_completions``.

        THE HANDOFF ROW IS CLOSED HERE, and only on real delivery. The accept
        path opens a row so an accepted-never-answered turn is detectable via
        ``HandoffStore.stale()``. If delivery fails we requeue the event and
        LEAVE THE ROW OPEN: a still-owed reply must keep looking owed. Marking
        it delivered on the attempt would trade a loud failure for a silent
        one, which is the trade every defect in this area has made.
        """
        await asyncio.sleep(3)  # let platforms finish connecting
        from tools.process_registry import process_registry as _pr

        while self._running:
            try:
                peer_events = _drain_peer_completions(_pr.completion_queue)
                for evt in peer_events:
                    try:
                        delivered = await self._deliver_peer_completion(evt)
                    except Exception as e:
                        _pr.completion_queue.put(evt)
                        logger.error("Peer completion injection error: %s", e)
                        continue
                    if delivered is False:
                        # Not delivered: put it back and leave the handoff row
                        # open so staleness still reports it as owed.
                        _pr.completion_queue.put(evt)
            except Exception as e:
                logger.debug("Peer completion watcher error: %s", e)
            await asyncio.sleep(interval)

    async def _deliver_peer_completion(self, evt: dict) -> bool:
        """Inject one finished peer turn into its session. True only on delivery.

        Returns False rather than raising when the target cannot be resolved,
        so the caller can requeue instead of losing the reply.
        """
        session_id = evt.get("session_id")
        text = (evt.get("text") or "").strip()
        if not session_id or not text:
            logger.warning(
                "peer completion event missing session_id or text; dropping to "
                "avoid an unroutable retry loop: keys=%s", sorted(evt)
            )
            return True  # unroutable forever; requeueing would spin

        # ROUTE FIRST. `session_id` names the session on THIS box that produced
        # the text — delivering there sends the answer to ourselves. When the
        # sender declared a return address, the reply belongs on the sender's
        # box and goes back over the peer channel.
        reply_to = evt.get("reply_to")
        if isinstance(reply_to, dict) and (reply_to.get("agent") or "").strip():
            # VALIDATE BEFORE DISPATCH. The check lives here, in the router,
            # not inside _return_peer_completion — a guard inside the method
            # that performs the action can be bypassed by anything that
            # replaces that method, including a test fake, which is exactly
            # how I first wrote it and exactly why F1 failed against my own
            # fixture. Validation belongs on the path every caller takes.
            if not self._reply_to_is_trustworthy(reply_to, evt):
                return True  # wrong forever; requeueing would spin
            try:
                ok = await self._return_peer_completion(reply_to, text, evt)
            except Exception:
                logger.exception(
                    "peer completion return-to-sender failed for %s", reply_to
                )
                return False
            if ok:
                self._close_peer_handoff(evt)
            return bool(ok)

        try:
            ok = await self._inject_peer_completion_turn(session_id, text, evt)
        except Exception:
            logger.exception("peer completion delivery failed for %s", session_id)
            return False
        if ok:
            handoff_id = evt.get("handoff_id")
            if handoff_id:
                try:
                    from gateway.handoff import DELIVERED, HandoffStore
                    from hermes_constants import get_hermes_home
                    import os as _os

                    store = HandoffStore(
                        _os.path.join(str(get_hermes_home()), "handoffs.jsonl"),
                        author="peer_completion_watcher",
                    )
                    store.record(handoff_id, DELIVERED)
                except Exception:
                    # The reply DID reach the human; failing to close the row
                    # leaves it looking owed, which is the safe direction.
                    logger.exception(
                        "delivered peer completion but could not close handoff %s",
                        handoff_id,
                    )
        return bool(ok)

    def _reply_to_is_trustworthy(self, reply_to: dict, evt: dict) -> bool:
        """Is this remote-supplied return address safe to deliver to?

        THE ADDRESS ARRIVES FROM THE NETWORK. `agent` goes straight into argv
        of a peer send, so an event that arrived FROM `wren` carrying
        reply_to={"agent": "someone-else"} would otherwise deliver the reply
        there, report success, and close the handoff row. Loud success, wrong
        destination — a silent misroute, which is the class this whole change
        exists to close, one level up. Found by Ash attacking PR #32.

        Not a shell-injection risk (create_subprocess_exec takes a list). The
        defect is granting a property that is never checked.

        This is checkable precisely BECAUSE the address is an agent NAME: a
        name can be compared against who actually sent the turn and against
        what this box already knows. An opaque session id could only be taken
        on faith.
        """
        agent = str(reply_to.get("agent") or "").strip()
        sender = str(evt.get("peer") or "").strip()

        # WHAT THIS CHECK CAN AND CANNOT DO — corrected after it refused a
        # legitimate reply on a live pair, 2026-09-16 11:31:46:
        #     reply_to.agent='wren' but the turn came from peer='peer'
        # The accept path defaults requesting_user to the literal "peer" when
        # the sender does not name itself, so the comparison could never match
        # and the ONLY thing the guard ever did in production was refuse a
        # real reply. It was written against a fixture that supplied a sender
        # name no real sender sends.
        #
        # Worse, the comparison is weak even when both fields are populated:
        # `peer` and `reply_to` arrive in the SAME request from the SAME
        # party, so agreement between them is self-attestation. A sender that
        # lies about one lies about both.
        #
        # So the load-bearing check is the one against LOCAL state: the agent
        # must be a peer THIS box already knows, and its host must match what
        # we recorded. identity.json is not attacker-supplied.
        if sender and sender != "peer" and agent != sender:
            logger.error(
                "peer completion declared reply_to.agent=%r but the turn came "
                "from peer=%r — refusing; dropping (handoff stays open)",
                agent, sender,
            )
            return False

        known = {}
        identity_readable = False
        try:
            import json as _json
            import os as _os

            from hermes_constants import get_hermes_home

            with open(
                _os.path.join(str(get_hermes_home()), "identity.json"),
                encoding="utf-8",
            ) as fh:
                known = _json.load(fh).get("peers") or {}
            identity_readable = True
        except Exception:
            identity_readable = False

        # "NO POLICY AVAILABLE" IS NOT "POLICY SAYS YES" — Ash's H1-H4 on #34.
        # In #32 the identity read was scoped to the host half, so a damaged
        # file disabled only the host comparison and the known-peer check
        # survived (his D2 asserted exactly that). Moving the read above the
        # known-peer check and returning True on failure widened the hole from
        # one comparison to the whole predicate, and H3/H4 reach it with NO
        # file damage at all: `if known and ...` short-circuits whenever the
        # peers map is missing or empty, leaving nothing checking the address
        # while every G arm stays green.
        #
        # So an unverifiable address is REFUSED, not trusted. A box that
        # cannot read its own peer list cannot know where a reply belongs —
        # the same reason _self_origin() returns None rather than guessing on
        # the sending side. The row stays open and the reply stays
        # recoverable; trusting instead would deliver it somewhere nobody
        # asked for and close the row saying it went home.
        if not identity_readable or not known:
            logger.error(
                "peer completion declared reply_to.agent=%r but this box "
                "cannot verify it (identity_readable=%s, known_peers=%d) — "
                "refusing; dropping (handoff stays open)",
                agent, identity_readable, len(known),
            )
            return False

        if agent not in known:
            logger.error(
                "peer completion declared reply_to.agent=%r which is not a "
                "known peer on this box (known: %s) — refusing; dropping "
                "(handoff stays open)", agent, sorted(known),
            )
            return False

        declared_host = str(reply_to.get("host") or "").strip().casefold()
        known_host = str(
            (known.get(agent) or {}).get("host") or ""
        ).strip().casefold()
        if declared_host and known_host and declared_host != known_host:
            logger.error(
                "peer completion from %r declared host %r but this box knows "
                "%r as %r — refusing; dropping (handoff stays open)",
                sender, declared_host, agent, known_host,
            )
            return False
        return True

    def _close_peer_handoff(self, evt: dict) -> None:
        """Close the handoff row after a delivery that actually happened.

        Failure to close is logged and swallowed: the reply DID reach its
        destination, so an open row means "looks owed when it isn't", which is
        the safe direction. The reverse — closing a row for an undelivered
        reply — is the silent failure this whole path exists to prevent.
        """
        handoff_id = evt.get("handoff_id")
        if not handoff_id:
            return
        try:
            from gateway.handoff import DELIVERED, HandoffStore
            from hermes_constants import get_hermes_home
            import os as _os

            store = HandoffStore(
                _os.path.join(str(get_hermes_home()), "handoffs.jsonl"),
                author="peer_completion_watcher",
            )
            store.record(handoff_id, DELIVERED)
        except Exception:
            logger.exception(
                "delivered peer completion but could not close handoff %s",
                handoff_id,
            )

    async def _return_peer_completion(
        self, reply_to: dict, text: str, evt: dict
    ) -> bool:
        """Send a finished turn back to the peer that asked for it.

        Uses `hermes peer dm` WITHOUT --no-wait deliberately. A reply is short
        and terminal: the far side records it and does not answer, so blocking
        for its accept costs nothing and gets a real success/failure. Using
        --no-wait here would make a returned reply that itself went missing
        indistinguishable from one that landed.

        Returns False on any failure so the caller requeues and the handoff
        row stays open.
        """
        agent = str(reply_to.get("agent") or "").strip()
        if not agent:
            return False
        import asyncio as _asyncio
        import shutil as _shutil

        exe = _shutil.which("hermes")
        if not exe:
            logger.error("cannot return peer completion: no hermes on PATH")
            return False
        body = f"[reply from {evt.get('peer') or 'peer'}]\n\n{text}"
        try:
            proc = await _asyncio.create_subprocess_exec(
                exe, "peer", "dm", agent, body,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
            )
            _out, err = await _asyncio.wait_for(proc.communicate(), timeout=120)
        except Exception:
            logger.exception("returning peer completion to %s raised", agent)
            return False
        if proc.returncode != 0:
            logger.error(
                "returning peer completion to %s failed rc=%s: %s",
                agent, proc.returncode, (err or b"").decode()[:300],
            )
            return False
        return True

    async def _inject_peer_completion_turn(
        self, session_id: str, text: str, evt: dict
    ) -> bool:
        """Append the peer's reply to its session as a real inbound turn.

        Uses the same mirror primitive the cron attach path uses, with
        ``role="user"``: the text is NOT this agent speaking. An
        assistant-role mirror of someone else's words replays as a genuine
        assistant turn and produces assistant->assistant pairs that break
        strict-alternation providers.
        """
        from gateway.mirror import mirror_to_session

        platform = evt.get("platform") or "api_server"
        chat_id = evt.get("chat_id") or session_id
        return bool(
            mirror_to_session(
                platform=platform,
                chat_id=chat_id,
                message_text=text,
                source_label=f"peer:{evt.get('peer') or 'unknown'}",
                role="user",
                session_id=session_id,
            )
        )

