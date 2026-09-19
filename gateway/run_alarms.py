"""Self-wake alarm firing for gateway sessions (``hermes_cli/alarms.py``, #53).

A coarse watcher scans every served profile's store for due alarms that carry a gateway ``route`` and
wakes the session they were set in, only while it is idle. CLI and TUI/desktop sessions (route-less
alarms) are fired by their own process; the claim is a compare-and-set, so an alarm fires once.

* Chat platforms: the note is injected as an internal event through the adapter, like a /loop
  tick: one user row (``internal_notification``), queued behind nothing because the session is idle,
  and a bare ``[SILENT]`` reply is not delivered (``gateway/response_filters.py``).
* Stateless adapters (api_server, e.g. an agent-to-agent Bot Chat): the note is self-posted into the
  session (``gateway.wake.deliver_wake``). That reply is not delivered anywhere, so the agent
  messages its peer itself when there is something to say.

An alarm fires only in the session it was set in: if the chat now routes to a different session
(it was suspended, or rotated without migration), the alarm stays pending until that session is
current again.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext, suppress
from typing import Any, Optional

logger = logging.getLogger("gateway.run")


class GatewayAlarmsMixin:
    """Watcher + firing for gateway-routed self-wake alarms."""

    async def _alarm_watcher(self, interval: float = 15.0) -> None:
        from gateway.run import _async_profile_runtime_scope, _handoff_watch_scopes

        await asyncio.sleep(5)  # let platforms finish connecting
        while self._running:
            try:
                for profile_name, profile_home in _handoff_watch_scopes(self):
                    scope = (_async_profile_runtime_scope(profile_home) if profile_home is not None
                             else nullcontext())
                    async with scope:
                        await self._alarm_scan_store(profile_name)
            except Exception as exc:
                logger.debug("alarm watcher error: %s", exc)
            await asyncio.sleep(interval)

    async def _alarm_scan_store(self, profile_name: Optional[str]) -> None:
        from hermes_cli.alarms import list_due_alarms

        await self._warm_goals_session_db("alarm watcher")
        for alarm in await self._run_in_executor_with_context(list_due_alarms):
            try:
                await self._alarm_fire_one(alarm, profile_name)
            except Exception as exc:
                logger.warning("alarm %s could not fire: %s", alarm.alarm_id, exc)

    async def _alarm_fire_one(self, alarm: Any, profile_name: Optional[str] = None) -> bool:
        """Wake ``alarm``'s session if it is idle and still current in its chat. True when fired."""
        from gateway.wake import adapter_supports_push
        from hermes_cli.alarms import claim_alarm, release_alarm, wake_notice

        route = alarm.route or {}
        platform_name = route.get("platform", "")
        if not platform_name:
            return False  # CLI / TUI / desktop alarm: its own process fires it
        profile = route.get("profile") or profile_name
        adapters = self._adapters_for_profile(profile)
        adapter = next((a for p, a in adapters.items() if p.value == platform_name), None)
        if adapter is None:
            return False

        if not adapter_supports_push(adapter):
            claimed = await self._run_in_executor_with_context(claim_alarm, alarm)
            if claimed is None:
                return False
            task = asyncio.create_task(self._alarm_self_post(adapter, claimed))
            # asyncio holds only a weak reference to a task; keep it until the turn finishes.
            tasks = self.__dict__.setdefault("_alarm_tasks", set())
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            return True

        if not route.get("chat_id"):
            return False
        source = self._build_process_event_source({
            "session_key": "", "platform": platform_name,
            **{k: route.get(k, "") for k in ("chat_id", "chat_type", "thread_id", "user_id", "user_name")},
        })
        if source is None:
            return False
        if profile and not getattr(source, "profile", None):
            source.profile = profile
        session_key = self._session_key_for_source(source)
        if session_key in self._running_agents:
            return False  # busy: stays due, the next scan retries once the turn ends
        if self.session_store.peek_session_id(session_key) != alarm.session_id:
            return False  # the chat routes elsewhere now; fire only in the session it was set in
        claimed = await self._run_in_executor_with_context(claim_alarm, alarm)
        if claimed is None:
            return False
        logger.info("alarm %s firing into %s chat=%s", claimed.alarm_id, platform_name, source.chat_id)
        try:
            await adapter.handle_message(self._synthetic_prompt_event(source, wake_notice(claimed), internal=True))
        except Exception:
            with suppress(Exception):
                await self._run_in_executor_with_context(release_alarm, claimed)
            raise
        return True

    async def _alarm_self_post(self, adapter: Any, alarm: Any) -> None:
        from gateway.wake import deliver_wake
        from hermes_cli.alarms import release_alarm, wake_notice

        try:
            await deliver_wake(adapter, text=wake_notice(alarm), session_id=alarm.session_id)
        except Exception as exc:
            logger.warning("alarm %s self-post failed: %s", alarm.alarm_id, exc)
            with suppress(Exception):
                await self._run_in_executor_with_context(release_alarm, alarm)
