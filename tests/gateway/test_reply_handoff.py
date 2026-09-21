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
    out = agent_notice("wren", "will", "Did the cycle land?", handoff_id=HID, reply_to="will")
    assert "tell_partner" in out
    assert "will" in out


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
