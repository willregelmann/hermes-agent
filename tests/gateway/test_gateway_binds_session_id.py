"""The gateway must bind HERMES_SESSION_ID for the turn, like every other surface.

`_set_session_env` passed `session_key=` but never `session_id=`, so `_SESSION_ID` was set to
`""`.  That is not "unbound" — `get_session_env` treats an explicitly-set `""` as authoritative
and SKIPS the `os.environ` fallback that `set_current_session_id` writes at agent build.  Any
tool resolving the session through the documented reader therefore saw no session at all, and
`alarm` refused with "This turn has no session to wake."

The bug only showed on turns that did NOT construct an agent: construction calls
`set_current_session_id` inside the bound scope and repairs the var, so a freshly built agent
could arm an alarm and the same session could not after an idle eviction rebuilt it.

A1/A2 are the defect arms, A3 pins the api_server sibling that always did it right, and B1/B2
keep the fix from being a blanket "never clear" (a cleared context must still read empty, or a
finished turn would leak its id into the next one).
"""

import pytest

from gateway.session_context import (
    clear_session_vars,
    get_session_env,
    reset_session_vars,
    set_current_session_id,
    set_session_vars,
)


@pytest.fixture(autouse=True)
def _isolate_context(monkeypatch):
    """Each case starts from "never bound here" with a clean environ."""
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    reset_session_vars()
    yield
    reset_session_vars()


def _bind_like_gateway(**overrides):
    """The gateway's own call shape (run.py::_set_session_env), overridable per case."""
    kwargs = dict(
        platform="google_chat",
        chat_id="spaces/XXXX",
        chat_type="dm",
        session_key="google_chat:spaces/XXXX",
        cron_session="",
    )
    kwargs.update(overrides)
    return set_session_vars(**kwargs)


def test_a1_gateway_bind_exposes_the_session_id():
    """A1: after a gateway-shaped bind, the session id is readable. RED before the fix."""
    _bind_like_gateway(session_id="20260831_210533_01629a01")
    assert get_session_env("HERMES_SESSION_ID") == "20260831_210533_01629a01"


def test_a2_an_omitted_session_id_is_a_cleared_var_not_a_fallback():
    """A2: pins WHY the call-site fix is the right one.

    ``set_session_vars`` sets every var it is given, and an omitted ``session_id`` becomes ``""``
    — authoritative, masking the ``os.environ`` value agent construction wrote.  That is correct
    and must stay (B1 depends on it: a cleared turn must not leak its id).  So the bug cannot be
    fixed by making the reader fall back harder; the binder has to pass the id.  A4 is the arm
    that proves it does.
    """
    set_current_session_id("20260831_210533_01629a01")  # what agent construction does
    _bind_like_gateway()  # a bind that forgets session_id, the pre-fix gateway shape
    assert get_session_env("HERMES_SESSION_ID") == "", (
        "an omitted session_id must stay authoritative-empty; the fix belongs at the call site"
    )


def test_a3_api_server_sibling_binds_it_explicitly():
    """A3: reference arm — the surface where alarm always worked passes session_id through."""
    set_session_vars(
        platform="api_server",
        chat_id="peer:ash",
        session_key="api:peer:ash",
        session_id="api_1789786014_c0a07a8f",
        async_delivery=False,
    )
    assert get_session_env("HERMES_SESSION_ID") == "api_1789786014_c0a07a8f"


def test_a4_the_production_binder_passes_it_through():
    """A4: drives the REAL gateway binder, not a local shim.

    A1-A3 exercise ``set_session_vars`` directly, so they would all stay green if
    ``_set_session_env`` never passed ``session_id`` — which is precisely the bug.  This arm
    calls the gateway's own method with a real SessionContext, so the fix cannot be inert.

    Platform.TELEGRAM rather than the reporting session's own Google Chat: that one is
    plugin-provided and absent from the core enum, and the binder is platform-agnostic.
    """
    from gateway.run import GatewayRunner
    from gateway.session import SessionContext, SessionSource
    from gateway.config import Platform

    context = SessionContext(
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="12345"),
        connected_platforms=[],
        home_channels={},
        session_key="telegram:12345",
        session_id="20260831_210533_01629a01",
    )
    runner = GatewayRunner.__new__(GatewayRunner)  # no I/O: only the binder is under test
    tokens = runner._set_session_env(context)
    try:
        assert get_session_env("HERMES_SESSION_ID") == "20260831_210533_01629a01"
    finally:
        clear_session_vars(tokens)


def test_b1_cleared_context_reads_empty_even_with_env_set():
    """B1: non-vacuity — clearing must still mask os.environ, or a finished turn leaks its id
    into whatever runs next in this task."""
    set_current_session_id("20260831_210533_01629a01")
    tokens = _bind_like_gateway(session_id="20260831_210533_01629a01")
    clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_ID") == ""


def test_b2_two_turns_do_not_bleed_into_each_other():
    """B2: a second turn's id must win, not the first one's."""
    _bind_like_gateway(session_id="session_one")
    assert get_session_env("HERMES_SESSION_ID") == "session_one"
    _bind_like_gateway(session_id="session_two")
    assert get_session_env("HERMES_SESSION_ID") == "session_two"
