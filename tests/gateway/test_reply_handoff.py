"""A handoff that asks a question must tell the receiving turn how to answer it.

Measured failure this fixes (2026-09-21): Wren asked Britta, through ``tell_partner``, whether she
wanted her own Google credentials. Her conversation answered in her own words and closed. The
asking session had no way to learn the answer, and had already promised Will it would report it.
Verbatim from her session (state.db id=23941): "Thanks Wren, I don't think I need that right now".
Nothing carried it back.

Two separate gaps, one per path:

A. ``human_notice`` never mentions a reply. It tells the receiving turn to act, to say where the
   handoff came from, or to answer ``[SILENT]``. A turn that does exactly as instructed leaves the
   asker waiting forever. The id is already in the envelope; only the instruction is missing.

B. ``agent_notice`` is worse: it carries NO handoff id at all, so a peer agent cannot quote a
   correlation token it was never given.

The reply target is the ``requester`` -- replying to them lands in the conversation that asked,
because that conversation IS the asker's conversation with them. That only holds when the requester
is a real partner name; when it is a bare display name from ``HERMES_SESSION_USER_NAME`` there is
nothing to address, and the notice must not invent a reply route.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from gateway.partner_handoff import agent_notice, human_notice  # noqa: E402


HID = "b8023a625375434ab96d3fc5cf93581b"


# --- A: the human path gains a reply instruction, and only when one is wanted ----------------

def test_a1_human_question_names_the_reply_tool_and_the_target():
    """The receiving turn is told HOW to answer and WHO to answer to."""
    out = human_notice(HID, "will", "britta", "Does she want her own calendar access?",
                       reply_to="will")
    assert "tell_partner" in out, "the notice must name the tool that carries an answer back"
    assert "will" in out
    assert HID[:8] in out, "the correlation id must still be in the envelope"


def test_a2_human_statement_is_unchanged_by_the_feature():
    """A handoff that is not a question must read exactly as it did before this change.

    Most handoffs are statements ('Britta asked you to say hi'). Telling every receiving turn to
    reply would turn one-way news into a volley -- the failure mode that made my 2000-char briefs
    spawn circular exchanges.
    """
    out = human_notice(HID, "will", "britta", "Britta asked you to say hi")
    assert "tell_partner" not in out
    assert "[SILENT]" in out
    assert out.startswith(f"[Handoff {HID[:8]} · from your conversation with will]")


def test_a3_no_reply_route_is_invented_without_a_partner_to_answer():
    """``requester`` can be a bare display name that is not in the partner directory.

    ``tell_partner_tool`` falls back to ``HERMES_SESSION_USER_NAME`` when ``partner_for_session``
    finds nothing. Addressing a reply to that name would emit an instruction the receiving turn
    cannot carry out -- a promise in the envelope, which is the whole class of bug this fixes.
    """
    out = human_notice(HID, "Some Person", "britta", "Do you want this?", reply_to="")
    assert "tell_partner" not in out


def test_a4_silent_stays_available_to_a_question():
    """A question still permits silence; the reply instruction must not read as compulsory."""
    out = human_notice(HID, "will", "britta", "Do you want this?", reply_to="will")
    assert "[SILENT]" in out


# --- B: the agent path carries the id it never had -------------------------------------------

def test_b1_agent_notice_carries_the_handoff_id():
    """A peer cannot quote a correlation token it was never sent."""
    out = agent_notice("wren", "will", "Please review #58", handoff_id=HID)
    assert HID[:8] in out


def test_b2_agent_question_names_the_reply_route():
    """The reply clause must name the route, and the assertion must read the CLAUSE.

    ``assert "will" in out`` was the original form and it cannot fail: the stamp already says
    "from wren's conversation with will", so the substring is present whatever the reply target
    is. Split the clause off and assert on it alone.
    """
    out = agent_notice("wren", "will", "Did the cycle land?", handoff_id=HID, reply_to="wren")
    clause = out.split("This one is a question:", 1)[1]
    assert "tell_partner('wren'" in clause
    assert "tell_partner('will'" not in clause


def test_b3_agent_statement_carries_the_id_but_asks_for_nothing():
    out = agent_notice("wren", "will", "Deployed d4b7b7d803", handoff_id=HID)
    assert HID[:8] in out
    assert "tell_partner" not in out


def test_b4_agent_notice_still_works_with_no_requester():
    """``agent_notice`` degrades to the bare agent name when there is no requester (existing
    behaviour at line 52; a session-less send records requester 'unknown')."""
    out = agent_notice("wren", "", "Something happened", handoff_id=HID)
    assert "wren" in out
    assert HID[:8] in out


# --- C: the regression that made this necessary ----------------------------------------------

def test_c1_the_britta_envelope_would_have_asked_for_an_answer():
    """Replay the real handoff. Under the old code this rendered with no reply route at all."""
    intent = ("Wren wants to ask Britta whether she would like her own Google account connected. "
              "It is fine to say no.")
    out = human_notice(HID, "will", "britta", intent, reply_to="will")
    assert "tell_partner" in out
    assert "will" in out
    assert intent in out, "the intent must still pass through verbatim"

# --- D: the PRODUCTION dispatcher, not just the notice builders -------------------------------

def test_d1_registry_handler_forwards_expect_reply():
    """The registered handler must pass the flag through.

    This is the arm that matters. A1-C1 exercise the notice builders directly and would stay green
    with the tool wired to ignore ``expect_reply`` entirely -- which is exactly how it was first
    written here: the schema advertised the field and the lambda dropped it, so the model could
    ask for a reply route and silently not get one. Same shape as #49's inert caller.
    """
    import tools.tell_partner_tool as tp
    from tools.registry import registry

    seen = {}

    def fake(partner, intent, expect_reply=False):
        seen.update(partner=partner, intent=intent, expect_reply=expect_reply)
        return "{}"

    real = tp.tell_partner
    tp.tell_partner = fake
    try:
        entry = registry.get("tell_partner") if hasattr(registry, "get") else None
        handler = getattr(entry, "handler", None) or registry._tools["tell_partner"].handler
        handler({"partner": "britta", "intent": "Do you want this?", "expect_reply": True})
    finally:
        tp.tell_partner = real
    assert seen["expect_reply"] is True, "the handler dropped expect_reply"


def test_d2_expect_reply_defaults_off_through_the_handler():
    """An ordinary statement must not acquire a reply route by default."""
    import tools.tell_partner_tool as tp
    from tools.registry import registry

    seen = {}

    def fake(partner, intent, expect_reply=False):
        seen.update(expect_reply=expect_reply)
        return "{}"

    real = tp.tell_partner
    tp.tell_partner = fake
    try:
        entry = registry.get("tell_partner") if hasattr(registry, "get") else None
        handler = getattr(entry, "handler", None) or registry._tools["tell_partner"].handler
        handler({"partner": "will", "intent": "Deployed."})
    finally:
        tp.tell_partner = real
    assert seen["expect_reply"] is False


# --- E: the CALL SITE, which is where the routing decision actually lives ---------------------

def test_e1_agent_path_addresses_the_reply_to_this_agent_not_the_requester():
    """The agent path must tell the peer to answer THIS AGENT, never the requester.

    B2 tests the notice builder, which takes ``reply_to`` as a parameter and will faithfully
    render whatever it is handed. The routing decision is at the CALL SITE, so only an arm that
    drives ``_deliver_to_agent`` can see it -- which is why the defect survived a suite that was
    green. Ash found it by rendering the string, not by reading the diff.

    Why the requester is wrong here and right on the human path: a name resolves in the
    directory of whoever is told to use it. Britta answering 'will' stays inside this agent and
    lands in the asking conversation. A PEER answering 'will' reaches ITS OWN conversation with
    Will -- valid, silent, and not the session that asked.
    """
    import gateway.partner_handoff as ph

    captured = {}

    def fake_send_to_peer(target, text):
        captured.update(target=target, text=text)
        return {"status": "queued"}

    class _Handoff:
        id = HID

    class _Store:
        def open_handoff(self, **kw):
            return _Handoff()

        def record(self, *a, **kw):
            return None

    import sys
    import types
    peer_mod = types.ModuleType("hermes_cli.subcommands.peer")
    peer_mod.send_to_peer = fake_send_to_peer
    handoff_mod = types.ModuleType("gateway.handoff")
    handoff_mod.DEFERRED = "deferred"
    handoff_mod.DELIVERED = "delivered"

    saved = {k: sys.modules.get(k) for k in ("hermes_cli.subcommands.peer", "gateway.handoff")}
    sys.modules["hermes_cli.subcommands.peer"] = peer_mod
    sys.modules["gateway.handoff"] = handoff_mod
    real_store = ph.handoff_store
    real_parent = ph.parent_handoff_id
    ph.handoff_store = lambda home: _Store()
    ph.parent_handoff_id = lambda store, sess: None
    try:
        ph.deliver_to_agent(
            partner="ash", peer_target="ash", intent="Did the cycle land?",
            from_session="s1", requester="will", self_agent="wren", home="/tmp",
            expect_reply=True)
    finally:
        ph.handoff_store = real_store
        ph.parent_handoff_id = real_parent
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    clause = captured["text"].split("This one is a question:", 1)[1]
    assert "tell_partner('wren'" in clause, (
        "the peer must be told to answer this agent; got: " + clause)
    assert "tell_partner('will'" not in clause, (
        "addressing the requester across an agent boundary reaches the PEER's own conversation "
        "with that human -- a third conversation that never saw the question")


# --- F: the plumbing BETWEEN the handler and the call site (mutation audit, wren:i27) ---------
#
# D1 fakes ``tell_partner`` whole and E1 calls ``deliver_to_agent`` directly, so nothing drove the
# three hops in between. Audited 2026-09-26: with expect_reply dropped at any of them
# (tell_partner -> deliver_to_agent, tell_partner -> _deliver_to_human, _deliver_to_human ->
# the coroutine) all twelve cases above stayed green -- the inert-caller shape D1's docstring
# says it guards, one layer further in. Same for the human call site's reply target (R3/R4) and
# the id inside the reply clause (R6). Each arm below was run against its mutant and failed.


def _clause(text):
    assert "This one is a question:" in text, "no reply clause rendered: " + text
    return text.split("This one is a question:", 1)[1]


def _patch_directory(monkeypatch, kind):
    import gateway.session_context as sc
    import hermes_cli.partners as hp

    entry = ({"kind": hp.AGENT, "peer": "ash"} if kind == "agent"
             else {"kind": hp.HUMAN, "primary": {"platform": "telegram", "chat_id": "c1"}})
    monkeypatch.setattr(hp, "load_partners", lambda: {"target": entry})
    monkeypatch.setattr(hp, "partner_for_session", lambda *a, **k: "will")
    monkeypatch.setattr(hp, "own_agent_name", lambda: "wren")
    monkeypatch.setattr(sc, "get_session_env", lambda name, default="": "s1" if name == "HERMES_SESSION_ID" else "")


def test_f1_tell_partner_forwards_expect_reply_to_the_agent_path(monkeypatch):
    import gateway.partner_handoff as ph
    import tools.tell_partner_tool as tp

    _patch_directory(monkeypatch, "agent")
    seen = []
    monkeypatch.setattr(ph, "deliver_to_agent", lambda **kw: seen.append(kw["expect_reply"]) or {"success": True})
    tp.tell_partner("target", "Did the cycle land?", expect_reply=True)
    tp.tell_partner("target", "Deployed.")
    assert seen == [True, False], f"tell_partner did not forward expect_reply to deliver_to_agent: {seen}"


def test_f2_tell_partner_forwards_expect_reply_to_the_human_path(monkeypatch):
    import tools.tell_partner_tool as tp

    _patch_directory(monkeypatch, "human")
    seen = []
    monkeypatch.setattr(tp, "_deliver_to_human",
                        lambda *a, expect_reply=False, **k: seen.append(expect_reply) or {"success": True})
    tp.tell_partner("target", "Do you want this?", expect_reply=True)
    tp.tell_partner("target", "Said hi.")
    assert seen == [True, False], f"tell_partner did not forward expect_reply to _deliver_to_human: {seen}"


def test_f3_deliver_to_human_wrapper_forwards_expect_reply_to_the_coroutine(monkeypatch):
    import asyncio
    import threading
    import gateway.partner_handoff as ph
    import gateway.run as gr
    import gateway.session_context as sc
    import tools.tell_partner_tool as tp

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    runner = type("R", (), {"_gateway_loop": loop})()
    seen = []

    async def fake_deliver(runner, **kw):
        seen.append(kw["expect_reply"])
        return {"success": True}

    monkeypatch.setattr(gr, "_gateway_runner_ref", lambda: runner, raising=False)
    monkeypatch.setattr(ph, "deliver_to_human", fake_deliver)
    monkeypatch.setattr(sc, "get_session_env", lambda name, default="": "")
    try:
        tp._deliver_to_human("britta", {}, "Do you want this?", "s1", "will", "/tmp", expect_reply=True)
        tp._deliver_to_human("britta", {}, "Said hi.", "s1", "will", "/tmp")
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
        loop.close()
    assert seen == [True, False], f"_deliver_to_human did not forward expect_reply: {seen}"


def _drive_real_deliver_to_human(monkeypatch, expect_reply):
    """Run the REAL deliver_to_human with every collaborator faked; return the notice it built."""
    import asyncio
    import sys
    import types
    import gateway.partner_handoff as ph
    import gateway.wake as gw
    from gateway.config import Platform

    captured = {}

    class _Handoff:
        id = HID

    class _Store:
        def open_handoff(self, **kw):
            return _Handoff()

        def record(self, *a, **kw):
            return None

    origin = types.SimpleNamespace(platform=Platform.TELEGRAM, chat_id="c1", thread_id=None,
                                   user_id=None, profile=None)
    entry = types.SimpleNamespace(origin=origin, session_id="s-britta", session_key="k", updated_at=1)

    class _Runner:
        session_store = types.SimpleNamespace(list_sessions=lambda: [entry])

        def _adapters_for_profile(self, profile):
            return {Platform.TELEGRAM: object()}

        def _synthetic_prompt_event(self, origin, text, internal=False):
            captured["text"] = text
            return types.SimpleNamespace()

    async def fake_admit(adapter, event):
        return None

    handoff_mod = types.ModuleType("gateway.handoff")
    handoff_mod.DEFERRED, handoff_mod.DELIVERED = "deferred", "delivered"
    monkeypatch.setitem(sys.modules, "gateway.handoff", handoff_mod)
    monkeypatch.setattr(gw, "adapter_supports_push", lambda a: True)
    monkeypatch.setattr(gw, "admit_internal_event", fake_admit)
    monkeypatch.setattr(ph, "handoff_store", lambda home: _Store())
    monkeypatch.setattr(ph, "parent_handoff_id", lambda store, sess: None)
    out = asyncio.run(ph.deliver_to_human(
        _Runner(), partner="britta", primary={"platform": "telegram", "chat_id": "c1"},
        intent="Do you want this?", from_session="s-will", requester="will", profile=None,
        home="/tmp", expect_reply=expect_reply))
    assert out.get("success"), out
    return captured["text"]


def test_f4_human_call_site_addresses_the_requester_when_a_reply_is_asked(monkeypatch):
    """On the human path the requester's name resolves inside THIS agent -- the asking
    conversation. Addressing the partner (britta) would tell her to message herself."""
    clause = _clause(_drive_real_deliver_to_human(monkeypatch, expect_reply=True))
    assert "tell_partner('will'" in clause, clause
    assert "tell_partner('britta'" not in clause, clause


def test_f5_human_call_site_asks_for_nothing_on_a_statement(monkeypatch):
    assert "tell_partner" not in _drive_real_deliver_to_human(monkeypatch, expect_reply=False)


def test_f6_the_reply_clause_itself_quotes_the_handoff_id():
    """A1/B1 find HID[:8] in the STAMP, so they stay green with the id dropped from the clause --
    the one place the replier is told what to quote."""
    for out in (human_notice(HID, "will", "britta", "Q?", reply_to="will"),
                agent_notice("wren", "will", "Q?", handoff_id=HID, reply_to="wren")):
        assert HID[:8] in _clause(out), out
