"""``tell_partner`` — hand an intent from this conversation to a partner's primary conversation (#52).

Britta asks Wren to say hi to Will: from the Britta conversation Wren calls
``tell_partner("will", "Britta asked you to say hi")``; Wren's conversation with Will wakes and says
it in its own words, stating where it came from. Partners are people or other agents, listed in
``partners:`` (``hermes_cli/partners.py``). Delivery lives in ``gateway/partner_handoff.py``; see
``docs/capabilities/tell-partner.md``.
"""

import asyncio
import json
import re

from tools.registry import registry, tool_error

HUMAN_DELIVERY_TIMEOUT_S = 60
# The compressor's cut markers, as they end a string: " ...[truncated]", " …[truncated]",
# "\n...[truncated]...". See agent/context_compressor.py.
_TRUNCATION_TAIL = re.compile(r"(?:\.\.\.|…)\s*\[truncated\](?:\.\.\.)?\s*$")


def tell_partner(partner: str, intent: str, expect_reply: bool = False) -> str:
    from gateway.session_context import get_session_env
    from hermes_cli.partners import AGENT, load_partners, own_agent_name, partner_for_session
    from hermes_constants import get_hermes_home

    name = (partner or "").strip().lower()
    intent = (intent or "").strip()
    if not name or not intent:
        return tool_error("tell_partner needs a partner (a name from your partner list) and an intent "
                          "(what is needed, in a sentence or two).")
    # Context compression cuts old tool-call args and appends "...[truncated]". A model that sees
    # its own earlier tell_partner call in that shape can imitate it and cut a fresh intent the
    # same way (measured on ha-pi: msg 23836, then three ~200-char intents). Sending one hands the
    # partner half a message and reports success. Anchored at the END: every compressor site
    # appends the marker, and an intent that merely mentions it (e.g. reporting this bug) is fine.
    if _TRUNCATION_TAIL.search(intent):
        return tool_error("This intent ends in a truncation marker ('...[truncated]'), so it was cut, "
                          "most likely copied from a compressed earlier call in your history. Nothing "
                          "was sent. Write the intent out in full and call tell_partner again.")
    partners = load_partners()
    entry = partners.get(name)
    if entry is None:
        known = ", ".join(sorted(partners)) or "none configured"
        return tool_error(f"No partner named {partner!r}. Your partners: {known}.")
    from_session = get_session_env("HERMES_SESSION_ID")
    requester = partner_for_session(
        from_session, get_session_env("HERMES_SESSION_PLATFORM"), get_session_env("HERMES_SESSION_CHAT_ID"),
        partners) or get_session_env("HERMES_SESSION_USER_NAME")
    home = str(get_hermes_home())

    if entry["kind"] == AGENT:
        from gateway.partner_handoff import deliver_to_agent
        result = deliver_to_agent(partner=name, peer_target=entry["peer"], intent=intent,
                                  from_session=from_session, requester=requester,
                                  self_agent=own_agent_name(), home=home, expect_reply=expect_reply)
    else:
        result = _deliver_to_human(name, entry["primary"], intent, from_session, requester, home,
                                   expect_reply=expect_reply)
    if "error" in result:
        return tool_error(result.pop("error"), **result)
    return json.dumps(result, ensure_ascii=False)


def _deliver_to_human(name: str, primary: dict, intent: str, from_session: str, requester: str, home: str,
                      expect_reply: bool = False) -> dict:
    """Run the delivery on the gateway's loop; a person is only reachable through the gateway."""
    from gateway.partner_handoff import deliver_to_human
    from gateway.session_context import get_session_env

    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
    except Exception:
        runner = None
    loop = getattr(runner, "_gateway_loop", None) if runner is not None else None
    if runner is None or loop is None:
        return {"error": f"People can only be reached from a conversation on the messaging gateway for now; "
                         f"this conversation isn't one. Reach {name} from one of your chat conversations."}
    coro = deliver_to_human(runner, partner=name, primary=primary, intent=intent, from_session=from_session,
                            requester=requester, profile=get_session_env("HERMES_SESSION_PROFILE") or None,
                            home=home, expect_reply=expect_reply)
    try:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=HUMAN_DELIVERY_TIMEOUT_S)
    except Exception as exc:
        return {"error": f"Could not hand this to {name}: {exc}"}


TELL_PARTNER_SCHEMA = {
    "name": "tell_partner",
    "description": (
        "Hand something to one of your partners (a person or another agent) in their own "
        "conversation with you. Pass an intent (what is needed), not a script: their "
        "conversation wakes and decides what to say."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "partner": {"type": "string", "description": "A name from your partner list."},
            "intent": {
                "type": "string",
                "description": "What is needed and who asked, e.g. 'Britta asked you to say hi to Will'.",
            },
            "expect_reply": {
                "type": "boolean",
                "description": (
                    "Set true when you are ASKING something and need the answer back. The handoff "
                    "is one-way: nothing in this conversation can observe their turn, so without "
                    "this their answer lands in their conversation and never reaches you. Their "
                    "notice then tells them to tell_partner the answer back to you. Leave false "
                    "for statements and requests that need no answer."
                ),
            },
        },
        "required": ["partner", "intent"],
    },
}


def check_partners_configured() -> bool:
    """Opt-in: shown only when this profile has a partner directory (``partners:`` in config.yaml)."""
    from hermes_cli.partners import load_partners
    return bool(load_partners())


registry.register(
    name="tell_partner", toolset="partners", schema=TELL_PARTNER_SCHEMA,
    check_fn=check_partners_configured,
    handler=lambda args, **kw: tell_partner(args.get("partner", ""), args.get("intent", ""),
                                            bool(args.get("expect_reply", False))),
    emoji="📨")
