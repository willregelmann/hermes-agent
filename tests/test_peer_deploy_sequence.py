"""Behaviour of the blue-green deploy sequence.

These tests are all about the FAILURE paths. A deploy that works is easy; the
properties worth pinning are the ones that only show up when something breaks:
rollback fires, a stale state file is not accepted as health, a self-reported
"connected" is not sufficient, and two deploys never run at once.

SSH is stubbed at ``_run`` so the sequence can be exercised without a second
machine. Everything above that boundary is the real code path.
"""

import time
from unittest.mock import patch

import pytest

from hermes_cli.subcommands import peer_deploy as pd
from hermes_cli.subcommands.peer_deploy import DeployError, DeployLock


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """Collapse the poll interval so tests do not sleep for real."""
    monkeypatch.setattr(pd, "HEALTH_POLL_S", 0)
    monkeypatch.setattr(pd, "HEALTH_TIMEOUT_S", 1)
    monkeypatch.setattr(time, "sleep", lambda *_: None)


@pytest.fixture(autouse=True)
def _no_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(pd, "LOCK_PATH", tmp_path / "deploy.lock")


def _state_probe(mtime, all_p, conn_p):
    return f"MTIME {mtime}\nALL {all_p}\nCONNECTED {conn_p}"


class TestHealthGate:
    """The gate exists to reject confident lies, not just missing answers."""

    def test_stale_state_file_is_not_health(self):
        """A file from the PREVIOUS process saying 'connected' must fail.

        This is the false green that has burned us repeatedly: the content is
        correct, the file simply describes a process that no longer exists.
        """
        issued = 1_000_000.0
        with patch.object(pd, "_run", return_value=(
                0, _state_probe(issued - 50, "google_chat", "google_chat"), "")):
            with pytest.raises(DeployError, match="did not become healthy"):
                pd._await_health("h", "system", issued)

    def test_listed_but_disconnected_platform_fails(self):
        """Wren's 55-minute outage: api_server up, google_chat listed and NOT
        connected. Presence in the platforms dict is not health."""
        issued = 1_000_000.0
        with patch.object(pd, "_run", return_value=(
                0, _state_probe(issued + 10, "api_server,google_chat", "api_server"), "")):
            with pytest.raises(DeployError, match="did not become healthy"):
                pd._await_health("h", "system", issued)

    def test_backoff_lines_in_log_fail_even_when_state_is_perfect(self):
        """The externally-observed gate. Self-reported state is fresh AND says
        everything is connected; the log says it is retrying. The log wins."""
        issued = 1_000_000.0

        def fake_run(host, cmd, timeout=120):
            if "gateway_state.json" in cmd:
                return 0, _state_probe(issued + 10, "google_chat", "google_chat"), ""
            if "is-active" in cmd:
                return 0, "active", ""
            if "journalctl" in cmd:
                return 0, "7", ""          # seven backoff lines since restart
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            with pytest.raises(DeployError, match="did not become healthy"):
                pd._await_health("h", "system", issued)

    def test_external_check_failure_fails_the_gate(self):
        """Wren's HA call. The gateway can be perfectly healthy and still be
        unable to do her actual job."""
        issued = 1_000_000.0

        def fake_run(host, cmd, timeout=120):
            if "gateway_state.json" in cmd:
                return 0, _state_probe(issued + 10, "google_chat", "google_chat"), ""
            if "is-active" in cmd:
                return 0, "active", ""
            if "journalctl" in cmd:
                return 0, "0", ""
            if "curl" in cmd:
                return 7, "", "connection refused"
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            with pytest.raises(DeployError, match="did not become healthy"):
                pd._await_health("h", "system", issued, ("curl -sf http://ha",))

    def test_all_gates_passing_returns(self):
        issued = 1_000_000.0

        def fake_run(host, cmd, timeout=120):
            if "gateway_state.json" in cmd:
                return 0, _state_probe(issued + 10, "api_server,google_chat",
                                       "api_server,google_chat"), ""
            if "is-active" in cmd:
                return 0, "active", ""
            if "journalctl" in cmd:
                return 0, "0", ""
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            pd._await_health("h", "system", issued)  # must not raise


class TestPreflight:
    def test_dirty_tree_refuses(self):
        with patch.object(pd, "_run", return_value=(0, "M some/file.py", "")):
            with pytest.raises(DeployError, match="uncommitted"):
                pd._preflight("h", "abc1234", allow_unresolved=False)

    def test_fetch_uses_an_explicit_ref(self):
        """Shallow clones make a bare `git fetch <remote>` return 0 and fetch
        nothing. The ref must be named or the deploy silently no-ops."""
        seen = []

        def fake_run(host, cmd, timeout=120):
            seen.append(cmd)
            if "status --porcelain" in cmd:
                return 0, "", ""
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            pd._preflight("h", "abc1234", allow_unresolved=False)

        fetches = [c for c in seen if "fetch" in c]
        assert fetches, "no fetch issued"
        assert f"{pd.FETCH_REMOTE} {pd.FETCH_REF}" in fetches[0], (
            f"fetch must name the ref explicitly, got: {fetches[0]!r}")

    def test_missing_sha_after_fetch_refuses(self):
        def fake_run(host, cmd, timeout=120):
            if "status --porcelain" in cmd:
                return 0, "", ""
            if "cat-file" in cmd:
                return 1, "", ""          # object not present
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            with pytest.raises(DeployError, match="not present"):
                pd._preflight("h", "deadbee", allow_unresolved=False)


class TestMutex:
    def test_second_concurrent_deploy_is_refused(self):
        """If both agents deploy at once and both brick, nobody can roll
        anybody back. That is the whole safety property."""
        with DeployLock("wren"):
            with pytest.raises(DeployError, match="another deploy is in progress"):
                with DeployLock("ash"):
                    pass

    def test_lock_is_released_on_error(self):
        with pytest.raises(RuntimeError):
            with DeployLock("wren"):
                raise RuntimeError("boom")
        with DeployLock("ash"):   # must not raise
            pass


class TestDeployRefusals:
    def test_unknown_peer_is_refused(self):
        with patch.object(pd, "_load_peers", create=True, return_value={}):
            with patch("hermes_cli.subcommands.peer._load_peers", return_value={}):
                assert pd.deploy("nobody", "abc1234") == 2

    def test_non_hex_sha_is_refused_before_any_ssh(self):
        peers = {"wren": {"url": "http://ha-pi.local:8642"}}
        with patch("hermes_cli.subcommands.peer._load_peers", return_value=peers):
            with patch.object(pd, "_run") as run:
                assert pd.deploy("wren", "main; rm -rf /") == 2
                run.assert_not_called()

    def test_self_target_is_refused_before_any_ssh(self):
        """The property everything rests on: an agent cannot deploy itself."""
        peers = {"me": {"url": "http://127.0.0.1:8642"}}
        with patch("hermes_cli.subcommands.peer._load_peers", return_value=peers):
            with patch.object(pd, "_run") as run:
                assert pd.deploy("me", "abc1234") == 2
                run.assert_not_called()


class TestRollbackIsNetworkIndependent:
    """Found by the first real scratch-target run, 2026-09-05.

    The deploy failed on `git fetch` (bad remote ref) and the ROLLBACK THEN
    FAILED THE SAME WAY, because rollback re-ran the identical preflight. A
    network fault was therefore sufficient to prevent recovery from a network
    fault, and the target was left stranded on the old SHA with the operator
    told 'may be down'. The SHA being rolled back TO is already local — it was
    checked out minutes earlier — so rollback must not touch the network.
    """

    def test_rollback_preflight_does_not_fetch(self):
        seen = []

        def fake_run(host, cmd, timeout=120):
            seen.append(cmd)
            if "status --porcelain" in cmd:
                return 0, "", ""
            if "fetch" in cmd:
                return 1, "", "fatal: couldn't find remote ref main"
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            pd._preflight("h", "abc1234", allow_unresolved=True, skip_fetch=True)

        assert not [c for c in seen if "fetch" in c], (
            "rollback preflight issued a fetch; a network fault would then "
            "block recovery from a network fault")

    def test_deploy_preflight_still_fetches(self):
        """The skip must be scoped to rollback only."""
        seen = []

        def fake_run(host, cmd, timeout=120):
            seen.append(cmd)
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            pd._preflight("h", "abc1234", allow_unresolved=False)

        assert [c for c in seen if "fetch" in c], "deploy must still fetch"

    def test_rollback_still_requires_the_object_locally(self):
        """Skipping the fetch must not skip the existence check."""
        def fake_run(host, cmd, timeout=120):
            if "status --porcelain" in cmd:
                return 0, "", ""
            if "cat-file" in cmd:
                return 1, "", ""
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            with pytest.raises(DeployError, match="not present"):
                pd._preflight("h", "abc1234", allow_unresolved=True, skip_fetch=True)


class TestRollbackNeverStrands:
    """Second finding from the scratch runs: the rollback gave up at the same
    step the deploy did, leaving the target unrestarted. Rollback's job is to
    get the peer RUNNING again, so it degrades rather than aborts."""

    def _runner(self, fail_sync):
        def fake_run(host, cmd, timeout=120):
            if "status --porcelain" in cmd:
                return 0, "", ""
            if "rev-parse" in cmd:
                return 0, "abc1234", ""
            if "uv sync" in cmd and fail_sync:
                return 1, "", "No `pyproject.toml` found"
            return 0, "", ""
        return fake_run

    def test_sync_failure_aborts_a_deploy(self):
        with patch.object(pd, "_run", side_effect=self._runner(True)):
            with pytest.raises(DeployError, match="uv sync failed"):
                pd._checkout_and_sync("h", "abc1234")

    def test_sync_failure_does_not_abort_a_rollback(self):
        """Must reach the restart. A running old version beats a dead one."""
        with patch.object(pd, "_run", side_effect=self._runner(True)):
            pd._checkout_and_sync("h", "abc1234", best_effort_sync=True)

    def test_checkout_failure_still_aborts_even_on_rollback(self):
        """Degrading on deps is deliberate; degrading on the wrong CODE is not."""
        def fake_run(host, cmd, timeout=120):
            if "checkout" in cmd:
                return 1, "", "pathspec did not match"
            return 0, "abc1234", ""
        with patch.object(pd, "_run", side_effect=fake_run):
            with pytest.raises(DeployError, match="checkout"):
                pd._checkout_and_sync("h", "abc1234", best_effort_sync=True)
