"""Both API-server refusals must log their decisive numbers (Ash, 2026-10-03).

Motivated by two real outages the same night, each of which cost about an hour
because the refusal left no receiver-side trace:

  429 window 02:46-03:06Z  five peer handoffs refused. The 429 predicate reads
      ``active_agent_work_count()`` = pending admissions + in-flight turns +
      live run tasks. The sender's box had no log line, and the only nearby
      metric (``active_agents`` in gateway_state.json) is a DIFFERENT quantity,
      so a correctly-timed sample of it was still invalid. Inflight during that
      window is unreconstructable: nothing logged it and
      ``_pending_agent_requests`` is not persisted.

  503 window 03:32-03:43Z  seven peer handoffs refused with ``gateway_draining``
      while ``/health`` served 200 throughout. The 503 went to the caller and
      never into the refusing box's log.

A single total would reproduce the ambiguity it was added to resolve, so the
429 line asserts the COMPONENTS separately.
"""
import logging

import pytest

from gateway.platforms.api_server import APIServerAdapter, _api_agent_request_reservation


class _Stub(APIServerAdapter):
    """Only the state the two refusal predicates read."""

    def __init__(self, *, pending=0, inflight=0, limit=0, draining=False):
        self._pending_agent_requests = pending
        self._inflight_agent_runs = inflight
        self._active_run_tasks = {}
        self._max_concurrent_runs = limit
        self._external_drain_active = draining

    @staticmethod
    def _gateway_is_draining_stub(value):
        return value


@pytest.fixture
def stub_draining(monkeypatch):
    def _apply(adapter, value):
        monkeypatch.setattr(
            APIServerAdapter, "_gateway_is_draining", staticmethod(lambda: value)
        )
        return adapter
    return _apply


def test_concurrency_429_logs_each_component_not_just_the_total(caplog):
    """The components must be individually recoverable from the log line.

    pending=7, inflight=2, tasks=0 all sum to 9, but so do 0/9/0 and 4/5/0.
    A log line that only says "inflight=9" cannot tell those apart, which is
    exactly the confusion that cost an hour on the 02:46Z window.
    """
    adapter = _Stub(pending=7, inflight=2, limit=5)

    with caplog.at_level(logging.WARNING, logger="gateway.platforms.api_server"):
        response = adapter._concurrency_limited_response()

    assert response is not None, "cap 5 with 9 live units must refuse"
    assert response.status == 429

    text = caplog.text
    assert text, "the 429 refusal emitted no log record at all"
    assert "pending=7" in text, f"pending component missing: {text!r}"
    assert "inflight_runs=2" in text, f"in-flight component missing: {text!r}"
    assert "run_tasks=0" in text, f"run-task component missing: {text!r}"
    assert "limit=5" in text, f"the limit that fired is missing: {text!r}"


def test_concurrency_429_does_not_log_when_it_admits(caplog):
    """Negative control: a line that fires on every request proves nothing."""
    adapter = _Stub(pending=1, inflight=0, limit=5)

    with caplog.at_level(logging.WARNING, logger="gateway.platforms.api_server"):
        response = adapter._concurrency_limited_response()

    assert response is None, "1 live unit under a cap of 5 must be admitted"
    assert "pending=" not in caplog.text, "logged a refusal it did not make"


def test_draining_503_logs_the_refusal(caplog, stub_draining):
    """The twelve-minute drain outage left no trace on the refusing box."""
    adapter = stub_draining(_Stub(draining=True), True)

    with caplog.at_level(logging.WARNING, logger="gateway.platforms.api_server"):
        response = adapter._draining_response()

    assert response is not None, "a draining gateway must refuse"
    assert response.status == 503
    assert "gateway_draining" in caplog.text, (
        f"the 503 refusal emitted no identifiable log record: {caplog.text!r}"
    )


def test_draining_503_silent_when_not_draining(caplog, stub_draining):
    """Negative control for the drain arm."""
    adapter = stub_draining(_Stub(draining=False), False)

    with caplog.at_level(logging.WARNING, logger="gateway.platforms.api_server"):
        response = adapter._draining_response()

    assert response is None
    assert "gateway_draining" not in caplog.text


@pytest.fixture
def active_reservation():
    """The caller holds its own admission slot, as inside a reserved request."""
    token = _api_agent_request_reservation.set({"active": True})
    try:
        yield
    finally:
        _api_agent_request_reservation.reset(token)


def _parse_fields(text):
    line = next(l for l in text.splitlines() if "rate_limit_exceeded" in l)
    body = line[line.index("(") + 1:line.rindex(")")]
    return dict(kv.split("=", 1) for kv in body.split())


def test_reservation_does_not_refuse_its_own_holder(caplog, active_reservation):
    """Components summing to exactly ``limit`` include the caller's own slot:
    the caller must be admitted, and nothing logged (Wren, review 5399008183)."""
    adapter = _Stub(pending=3, inflight=2, limit=5)

    with caplog.at_level(logging.WARNING, logger="gateway.platforms.api_server"):
        response = adapter._concurrency_limited_response()

    assert response is None, "the caller's own reservation refused the caller"
    assert "rate_limit_exceeded" not in caplog.text


def test_reservation_refusal_line_reconciles_with_itself(caplog, active_reservation):
    """With a reservation active the raw components sum to effective_inflight + 1;
    the line must carry reservation_active so a reader can reconcile it instead of
    guessing between the reservation and a sampling skew."""
    adapter = _Stub(pending=4, inflight=2, limit=5)

    with caplog.at_level(logging.WARNING, logger="gateway.platforms.api_server"):
        response = adapter._concurrency_limited_response()

    assert response is not None and response.status == 429
    f = _parse_fields(caplog.text)
    assert f["reservation_active"] == "True", f
    parts = int(f["pending"]) + int(f["inflight_runs"]) + int(f["run_tasks"])
    assert parts == int(f["effective_inflight"]) + 1, f


def test_no_reservation_line_reconciles_exactly(caplog):
    """Control for the reconciliation arm: without a reservation the components
    sum to effective_inflight exactly and the flag says so."""
    adapter = _Stub(pending=7, inflight=2, limit=5)

    with caplog.at_level(logging.WARNING, logger="gateway.platforms.api_server"):
        adapter._concurrency_limited_response()

    f = _parse_fields(caplog.text)
    assert f["reservation_active"] == "False", f
    parts = int(f["pending"]) + int(f["inflight_runs"]) + int(f["run_tasks"])
    assert parts == int(f["effective_inflight"]), f
