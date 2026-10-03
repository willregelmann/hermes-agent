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

        # A RETURN ADDRESS NAMING THIS BOX IS REFUSED, WITH ITS OWN MESSAGE.
        # 63 of the 118 refusals measured on will-MS-7B93 were reply_to.agent
        # naming the box itself. Trusting it does not help: the router sends
        # every trusted reply_to over the wire (`hermes peer dm <own name>`),
        # which fails, returns False, and the watcher requeues every interval
        # forever (Foil's B1 on #81). There is no defined local target either:
        # session_id is where the turn RAN, so injecting there answers
        # ourselves. Until a local target exists, refuse once, distinctly.
        if agent and agent.casefold() == self._own_agent_name().casefold():
            logger.error(
                "peer completion reply_to names this box (%r) — refusing; "
                "dropping (handoff stays open). No local target is defined "
                "for a self-addressed return", agent,
            )
            return False

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

        # TWO TABLES, TWO QUESTIONS. `bot_peers` is the authority for "is this
        # a known peer" because it is the table the peer links actually live
        # in — `_load_peers()` is the same reader the SENDING side uses, so the
        # two sides cannot disagree about who is a peer. identity.json answers
        # only "what host should this peer be claiming".
        #
        # WHY: measured 118 refusals on will-MS-7B93 between 2026-09-30
        # 22:18:43Z and 2026-10-02 12:54:41Z (ash 63, refsdal 28, pair 27),
        # every one logging `(known: ['wren'])`. The known-peer arm read
        # identity.json's peers map, which held one name, while bot_peers held
        # four and live traffic named five. The maps diverged and the return
        # leg failed closed on every name the older map never learned. Wren
        # measured the same divergence on ha-pi (identity ['ash'] vs five in
        # bot_peers). Two tables that must AGREE will diverge; one table that
        # is ENRICHED by another cannot.
        peers = {}
        try:
            from hermes_cli.subcommands.peer import _load_peers

            peers = _load_peers() or {}
        except Exception:
            logger.exception(
                "peer completion could not read bot_peers; treating the peer "
                "table as empty (refuses rather than trusts)"
            )
            peers = {}

        identity_peers = {}
        try:
            import json as _json
            import os as _os

            from hermes_constants import get_hermes_home

            with open(
                _os.path.join(str(get_hermes_home()), "identity.json"),
                encoding="utf-8",
            ) as fh:
                identity_peers = _json.load(fh).get("peers") or {}
        except Exception:
            # Not fatal, and deliberately so: a missing or damaged
            # identity.json costs the HOST comparison, not the whole predicate.
            # The `pair` profile on will-MS-7B93 has no identity.json at all,
            # and refusing every return for that reason is precisely the
            # outage this change removes.
            identity_peers = {}

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
        if not peers:
            logger.error(
                "peer completion declared reply_to.agent=%r but this box has "
                "no peer table to verify it against (bot_peers empty or "
                "unreadable) — refusing; dropping (handoff stays open)", agent,
            )
            return False

        if agent not in peers:
            logger.error(
                "peer completion declared reply_to.agent=%r which is not a "
                "known peer on this box (known: %s) — refusing; dropping "
                "(handoff stays open)", agent, sorted(peers),
            )
            return False

        # THE HOST ARM COMPARES AN IDENTITY, NEVER A ROUTE. `bot_peers[x].url`
        # answers "how do I reach x FROM HERE" — local, topology-dependent,
        # and legitimately rewritten: PLAN-refsdal-draft13 step 7 rewrites the
        # sandbox's bot_peers.ash.url to http://127.0.0.1:8642 because
        # systemd-resolved answers the mDNS name with a docker-bridge address
        # in there. Deriving the expected host from that URL would compare
        # 127.0.0.1 against ash's declared will-MS-7B93.local and refuse 100%
        # of returns on that edge. identity.json records what the peer SAYS it
        # is, which is the thing an anti-spoof check can pin. Wren's
        # counterexample; see tests/gateway/test_peer_return_known_peer_table.
        #
        # WHETHER THE ARM IS LIVE TURNS ON KNOWLEDGE OF *THIS PEER*, NOT ON THE
        # BOX'S KNOWLEDGE IN GENERAL. Wren's ruling, and she had to correct me
        # twice to get here. Her first formulation keyed off whether the box
        # records ANY peer host; she replaced it in the same handoff with the
        # per-peer rule after the Refsdal counterexample, and confirmed it when
        # my per-box implementation left a test red. Measured: all 118 refusals
        # on will-MS-7B93 landed on the `default` profile, whose identity.json
        # records a host for `wren` and for nobody else. Under a PER-BOX rule
        # that profile's arm is live for every name, so refsdal (28) and pair
        # (27) stay refused — 55 of the 118 survive the fix. Under the PER-PEER
        # rule their arm is inert and they deliver with a WARNING, which closes
        # all 118. A host recorded for one peer is not evidence about another.
        #
        # An empty peers map, a missing identity.json, and a map that simply
        # lacks this peer are therefore all ONE proposition: no identity
        # knowledge about THIS peer. Keying off the file, or off the box's
        # aggregate knowledge, refuses returns to peers nobody ever described.
        declared_host = str(reply_to.get("host") or "").strip().casefold()
        known_host = str(
            (identity_peers.get(agent) or {}).get("host") or ""
        ).strip().casefold()
        route = (peers.get(agent) or {}).get("url") or "<none>"

        if not known_host:
            # OUTCOME 4, now the ONLY no-knowledge outcome: no host recorded
            # for THIS peer, whatever is known about others. The supplementary
            # check has nothing to compare against, which is a different
            # proposition from "no policy" (outcome 2) — policy lives in
            # bot_peers and said yes. Refusing here is what left 55 of the 118
            # refusals in place on my first implementation.
            # A box that records a host for `wren` and none for `refsdal` is
            # not withholding a vouch for refsdal; it simply never learned one.
            logger.warning(
                "peer completion from %r declared host %r for %r, but this box "
                "records no identity host for that peer — host arm inert, "
                "delivering unverified (record one in identity.json to enable "
                "the check; known: %s; route: %s)",
                sender, declared_host or "<none>", agent,
                sorted(identity_peers) or "<none>", route,
            )
            return True

        if declared_host and declared_host != known_host:
            # OUTCOME 3a: the anti-spoof arm firing. Print all three facts —
            # declared, recorded, route — because none of the 118 refusal
            # lines could tell a stale identity.json from a spoof attempt from
            # a topology change under us.
            logger.error(
                "peer completion from %r declared host %r but this box knows "
                "%r as %r (route: %s) — refusing; dropping (handoff stays "
                "open)",
                sender, declared_host, agent, known_host, route,
            )
            return False

        if not declared_host:
            # OUTCOME 3b: WITHHOLDING, which is not the same as CONTRADICTING.
            # Wren's finding (handoff 3a37322d). The box knows what this peer's
            # host should be, and the sender declined to say — so the `and` in
            # the 3a test above short-circuits and, before this arm existed,
            # fell straight through to `return True`: delivered, SILENTLY, with
            # no log at any level. That made an ABSENT attacker-controlled
            # field quieter than outcome 4, where the box knows nothing at all
            # and still warns. Exactly inverted: the more this box knows, the
            # less it said.
            #
            # Refusing rather than warning, because the two cases are different
            # propositions. Outcome 4 delivers because the box has nothing to
            # check against; here it HAS the knowledge and the sender withheld
            # the one field the check needs. A fail-closed predicate cannot
            # treat "declined to answer" as "nothing to verify".
            #
            # NOT unreachable, and Wren's own premise for calling this
            # non-blocking does not hold — I read `_self_origin()` rather than
            # taking it: peer.py:86-88 builds `{"agent": ...}` and adds "host"
            # ONLY `if host`, where host falls back to socket.gethostname().
            # With no host in identity.json AND gethostname() raising or
            # returning "", a LEGITIMATE sender emits agent-without-host.
            # Measured on both arms. Rare, but not hand-crafted-only.
            logger.error(
                "peer completion from %r declared NO host but this box knows "
                "%r as %r (route: %s) — refusing; dropping (handoff stays "
                "open). Withholding the field is not the same as this box "
                "having no record of it",
                sender, agent, known_host, route,
            )
            return False
        return True

    def _own_agent_name(self) -> str:
        """This box's own declared agent name, or "" when it has none.

        Deliberately shares `_self_origin()` with the SENDING side rather than
        re-reading identity.json: the two must agree about who this box is, and
        two readers of one file are two chances to disagree. `_self_origin()`
        returns None when the name is blank ("say nothing rather than invent
        one"), and "" here can never equal a non-empty reply_to.agent, so an
        identity-less box simply never takes the self path.
        """
        try:
            from hermes_cli.subcommands.peer import _self_origin

            return str((_self_origin() or {}).get("agent") or "").strip()
        except Exception:
            return ""

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
        # The label names who is replying: THIS agent. ``evt["peer"]`` is the agent that asked, and
        # stating that as the origin tells the receiver it is reading its own words.
        from hermes_cli.partners import own_agent_name
        body = f"[reply from {own_agent_name() or 'peer'}]\n\n{text}"
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

