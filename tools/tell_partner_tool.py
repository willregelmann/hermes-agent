"""``tell_partner`` — hand an intent from this conversation to a partner's primary conversation (#52).

Britta asks Wren to say hi to Will: from the Britta conversation Wren calls
``tell_partner("will", "Britta asked you to say hi")``; Wren's conversation with Will wakes and says
it in its own words, stating where it came from. Partners are people or other agents, listed in
``partners:`` (``hermes_cli/partners.py``). Delivery lives in ``gateway/partner_handoff.py``; see
``docs/capabilities/tell-partner.md``.
"""

import asyncio
import json

from tools.registry import registry, tool_error

HUMAN_DELIVERY_TIMEOUT_S = 60


def tell_partner(partner: str, intent: str) -> str:
    from gateway.session_context import get_session_env
    from hermes_cli.partners import AGENT, load_partners, own_agent_name, partner_for_session
    from hermes_constants import get_hermes_home

    name = (partner or "").strip().lower()
    intent = (intent or "").strip()
    if not name or not intent:
        return tool_error("tell_partner needs a partner (a name from your partner list) and an intent "
                          "(what is needed, in a sentence or two).")
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
                                  self_agent=own_agent_name(), home=home)
    else:
        result = _deliver_to_human(name, entry["primary"], intent, from_session, requester, home)
    if "error" in result:
        return tool_error(result.pop("error"), **result)
    return json.dumps(result, ensure_ascii=False)


def _deliver_to_human(name: str, primary: dict, intent: str, from_session: str, requester: str, home: str) -> dict:
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
                            home=home)
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
    handler=lambda args, **kw: tell_partner(args.get("partner", ""), args.get("intent", "")),
    emoji="📨")
