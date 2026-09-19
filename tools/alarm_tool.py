"""``alarm`` — set, list or cancel a one-shot self-wake alarm for the current session (#53).

"I'll check back in an hour" has to be something the agent arranges, not only something it says.
An alarm wakes THIS session later, starting from a note the agent writes now. Store, firing and
semantics live in ``hermes_cli/alarms.py``; see ``docs/capabilities/self-wake.md``.
"""

import json
from datetime import datetime

from tools.registry import registry, tool_error


def _session_route() -> dict:
    """Gateway routing for the alarm, captured only inside the gateway process (which fires it).
    CLI / TUI / desktop sessions get an empty route and are fired by their own process."""
    from gateway.session_context import get_session_env
    try:
        from gateway.run import _gateway_runner_ref
        in_gateway = _gateway_runner_ref() is not None
    except Exception:
        in_gateway = False
    platform = get_session_env("HERMES_SESSION_PLATFORM")
    if not (in_gateway and platform):
        return {}
    fields = {"platform": platform, "chat_id": "HERMES_SESSION_CHAT_ID", "chat_type": "HERMES_SESSION_CHAT_TYPE",
              "thread_id": "HERMES_SESSION_THREAD_ID", "user_id": "HERMES_SESSION_USER_ID",
              "user_name": "HERMES_SESSION_USER_NAME", "profile": "HERMES_SESSION_PROFILE"}
    return {k: (v if k == "platform" else get_session_env(v)) for k, v in fields.items()}


def _wakeable_session() -> tuple:
    """(session_id, error): the session an alarm would wake, or why this turn has none."""
    from agent.delegation_context import is_delegated_child_context
    from gateway.session_context import get_session_env
    if get_session_env("HERMES_CRON_SESSION"):
        return "", ("A cron run ends when it finishes, so nothing would be woken. Schedule another "
                    "cron job instead.")
    if is_delegated_child_context():
        return "", ("A subagent's session isn't woken later. Tell the parent what should be "
                    "followed up; it can set the alarm in its own conversation.")
    session_id = get_session_env("HERMES_SESSION_ID")
    if not session_id:
        return "", "This turn has no session to wake."
    return session_id, ""


def _describe(alarm) -> dict:
    import time
    from hermes_cli.alarms import _span
    from hermes_time import now as hermes_now
    due = datetime.fromtimestamp(alarm.due_at, tz=hermes_now().tzinfo)
    return {"alarm_id": alarm.alarm_id, "note": alarm.note,
            "due_at": due.isoformat(timespec="minutes"), "due_in": _span(alarm.due_at - time.time())}


def alarm_tool(action: str, when: str = "", note: str = "", alarm_id: str = "") -> str:
    from hermes_cli.alarms import AlarmError, cancel_alarm, list_alarms, set_alarm

    session_id, err = _wakeable_session()
    if err:
        return tool_error(err)
    action = (action or "").strip().lower()
    if action == "set":
        try:
            alarm = set_alarm(session_id, when, note, route=_session_route())
        except AlarmError as exc:
            return tool_error(str(exc))
        return json.dumps({"success": True, **_describe(alarm),
                           "wakes": "this conversation, starting from your note"}, ensure_ascii=False)
    if action == "list":
        return json.dumps({"success": True, "alarms": [_describe(a) for a in list_alarms(session_id)]},
                          ensure_ascii=False)
    if action == "cancel":
        if not (alarm_id or "").strip():
            return tool_error("cancel needs alarm_id (see alarm(action='list')).")
        if not cancel_alarm(session_id, alarm_id.strip()):
            return tool_error(f"No pending alarm {alarm_id} in this conversation.")
        return json.dumps({"success": True, "cancelled": alarm_id.strip()})
    return tool_error("action must be one of: set, list, cancel.")


ALARM_SCHEMA = {
    "name": "alarm",
    "description": (
        "Set, list or cancel a one-shot alarm that wakes THIS conversation later, e.g. to check "
        "a deploy in an hour. When it goes off you take another turn here, with the full "
        "history, starting from your note."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["set", "list", "cancel"]},
            "when": {
                "type": "string",
                "description": "For set: 'in 20m', 'in 1h', 'in 1d', 'at 17:00', 'at 5pm', or an ISO timestamp.",
            },
            "note": {
                "type": "string",
                "description": "For set: what your future self should check or do, and why.",
            },
            "alarm_id": {"type": "string", "description": "For cancel: the alarm to cancel."},
        },
        "required": ["action"],
    },
}


registry.register(
    name="alarm", toolset="alarms", schema=ALARM_SCHEMA,
    handler=lambda args, **kw: alarm_tool(
        args.get("action", ""), args.get("when", ""), args.get("note", ""), args.get("alarm_id", "")),
    emoji="⏰")
