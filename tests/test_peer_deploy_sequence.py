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
            if "UV_NOT_FOUND" in cmd:
                return 0, "UV /usr/bin/uv", ""
            if "importlib.metadata" in cmd:
                # A real venv is never empty; an empty inventory is now
                # correctly refused, so the fixture must be realistic.
                return 0, 'JSON {"numpy": "2.4.6"}', ""
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


class TestPackagesRemovedBySyncAreRestored:
    """Regression for the 2026-09-06 incident.

    `uv sync --extra all --locked` rebuilt the venv and removed numpy,
    onnxruntime, tokenizers (semantic memory), google-cloud-pubsub (Google Chat
    transport), pip and pytest. Nothing errored: sync returned 0, the gateway
    started, every health gate passed. One agent was left unreachable and could
    not report it, because the thing removed WAS the way it reports.

    Note the approach: measure before and after, do not predict. An earlier
    version of this fix diffed against uv.lock and was WRONG — 4 of the 5
    casualties are named in uv.lock and were removed anyway.
    """

    def test_uv_path_is_discovered_not_assumed(self):
        """~/.hermes/bin/uv here, ~/.local/bin/uv on the Pi."""
        def fake_run(host, cmd, timeout=120):
            if "UV_NOT_FOUND" in cmd:          # the probe
                return 0, "UV /home/will/.hermes/bin/uv", ""
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            assert pd._find_uv("h") == "/home/will/.hermes/bin/uv"

    def test_missing_uv_is_a_clear_error(self):
        with patch.object(pd, "_run", return_value=(0, "UV_NOT_FOUND", "")):
            with pytest.raises(DeployError, match="uv not found"):
                pd._find_uv("h")

    def test_uv_probe_does_not_depend_on_PATH(self):
        """ssh gives a NON-LOGIN shell: ~/.profile is never sourced, so the
        PATH entry that makes uv work in a terminal does not exist. The first
        real peer deploy failed here — uv sync never ran at all.

        Absolute candidates must be tried BEFORE falling back to command -v.
        """
        probe = {}

        def fake_run(host, cmd, timeout=120):
            probe["cmd"] = cmd
            return 0, "UV /home/will/.hermes/bin/uv", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            pd._find_uv("h")

        c = probe["cmd"]
        assert "$HOME/.hermes/bin/uv" in c, "must try absolute paths"
        assert "$HOME/.local/bin/uv" in c, "must try the Pi's path too"
        assert c.index("$HOME/.hermes/bin/uv") < c.index("command -v"), (
            "absolute candidates must precede the PATH fallback")
        assert "~/" not in c, (
            "tilde may not expand inside a quoted ssh command; use $HOME")

    def test_empty_output_is_not_read_as_success(self):
        """A `for` loop that finds nothing still exits 0. Without an explicit
        marker, 'not found' and 'found' differ only by empty stdout — the same
        silent-falsy shape as the empty-inventory bug."""
        with patch.object(pd, "_run", return_value=(0, "", "")):
            with pytest.raises(DeployError, match="uv not found"):
                pd._find_uv("h")

    def test_packages_dropped_by_sync_are_reinstalled_at_pinned_versions(self):
        order = []
        before = {"numpy": "2.4.6", "google-cloud-pubsub": "2.39.0",
                  "requests": "2.32.0", "pip": "25.0"}
        after = {"requests": "2.32.0", "pip": "25.0"}   # sync dropped two
        state = {"synced": False}

        def fake_run(host, cmd, timeout=120):
            import json as _j
            if "checkout" in cmd:
                order.append("checkout")
            elif "rev-parse" in cmd:
                return 0, "abc1234", ""
            elif "UV_NOT_FOUND" in cmd:
                return 0, "UV /usr/bin/uv", ""
            elif "importlib.metadata" in cmd:
                order.append("measure")
                snap = after if state["synced"] else before
                return 0, "JSON " + _j.dumps(snap), ""
            elif "sync --extra" in cmd:
                order.append("sync")
                state["synced"] = True
            elif "pip install" in cmd:
                order.append("restore")
                assert "numpy==2.4.6" in cmd, cmd
                assert "google-cloud-pubsub==2.39.0" in cmd, cmd
                assert "requests" not in cmd, "survivors must not be reinstalled"
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            pd._checkout_and_sync("h", "abc1234")

        assert order.index("measure") < order.index("sync")
        assert order.index("restore") > order.index("sync")

    def test_nothing_removed_means_no_reinstall(self):
        # Essentials must be present or the essentials check fires (correctly).
        same = {"numpy": "2.4.6", "pip": "25.0"}
        with patch.object(pd, "_run") as run:
            pd._restore_removed("h", same, same, "/usr/bin/uv")
            run.assert_not_called()

    def test_upgraded_package_is_not_downgraded(self):
        """Only DISAPPEARANCES are repaired; a version bump is left alone."""
        with patch.object(pd, "_run") as run:
            pd._restore_removed("h",
                                {"numpy": "2.4.6", "pip": "25.0"},
                                {"numpy": "2.5.0", "pip": "25.0"},
                                "/usr/bin/uv")
            run.assert_not_called()

    def test_restore_failure_is_loud(self):
        def fake_run(host, cmd, timeout=120):
            if "pip install" in cmd:
                return 1, "", "network unreachable"
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            with pytest.raises(DeployError, match="uv sync removed"):
                pd._restore_removed("h", {"numpy": "2.4.6", "pip": "25.0"},
                                    {"pip": "25.0"}, "/usr/bin/uv")


class TestUniversalHealthChecks:
    """'Configured' is not 'working'. These import the real modules."""

    def test_dead_memory_deps_fail_the_gate(self):
        issued = 1_000_000.0

        def fake_run(host, cmd, timeout=120):
            if "gateway_state.json" in cmd:
                return 0, _state_probe(issued + 10, "google_chat", "google_chat"), ""
            if "is-active" in cmd:
                return 0, "active", ""
            if "journalctl" in cmd:
                return 0, "0", ""
            if "import numpy" in cmd:
                return 1, "", "ModuleNotFoundError: No module named 'numpy'"
            return 0, "", ""

        checks = tuple(c.format(repo=pd.REPO) for _, c in pd.UNIVERSAL_CHECKS)
        with patch.object(pd, "_run", side_effect=fake_run):
            with pytest.raises(DeployError, match="did not become healthy"):
                pd._await_health("h", "system", issued, checks)

    def test_dead_chat_transport_fails_the_gate(self):
        """The exact silence: platforms report connected, pubsub is gone."""
        issued = 1_000_000.0

        def fake_run(host, cmd, timeout=120):
            if "gateway_state.json" in cmd:
                return 0, _state_probe(issued + 10, "google_chat", "google_chat"), ""
            if "is-active" in cmd:
                return 0, "active", ""
            if "journalctl" in cmd:
                return 0, "0", ""
            if "pubsub_v1" in cmd:
                return 1, "", "ImportError"
            return 0, "", ""

        checks = tuple(c.format(repo=pd.REPO) for _, c in pd.UNIVERSAL_CHECKS)
        with patch.object(pd, "_run", side_effect=fake_run):
            with pytest.raises(DeployError, match="did not become healthy"):
                pd._await_health("h", "system", issued, checks)

    def test_both_universal_checks_present(self):
        names = [n for n, _ in pd.UNIVERSAL_CHECKS]
        assert "memory provider deps" in names
        assert "chat transport" in names


class TestInventoryFailureIsNotSilent:
    """Wren caught this in review, before it ran on a live host.

    _installed_packages() originally returned {} when its probe failed. Then
    `before` and `after` are both empty, their difference is empty, and
    _restore_removed() does nothing — and reports success. The guard against
    silent breakage would itself have failed silently, which is the exact bug
    class it exists to prevent.

    Her sharper point was about the rollback path: there the peer would be told
    it had recovered while missing the transport it would use to say otherwise.
    """

    def _probe_fails(self, host, cmd, timeout=120):
        if "importlib.metadata" in cmd:
            return 1, "", "python3: command not found"
        if "UV_NOT_FOUND" in cmd:
            return 0, "UV /usr/bin/uv", ""
        if "rev-parse" in cmd:
            return 0, "abc1234", ""
        return 0, "", ""

    def test_unreadable_inventory_raises_rather_than_returning_empty(self):
        with patch.object(pd, "_run", side_effect=self._probe_fails):
            with pytest.raises(DeployError, match="could not read the installed"):
                pd._installed_packages("h")

    def test_empty_inventory_is_refused(self):
        """A working venv always has packages. Zero means the probe lied."""
        def fake_run(host, cmd, timeout=120):
            if "importlib.metadata" in cmd:
                return 0, "JSON {}", ""
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            with pytest.raises(DeployError, match="ZERO installed packages"):
                pd._installed_packages("h")

    def test_malformed_inventory_is_refused(self):
        def fake_run(host, cmd, timeout=120):
            if "importlib.metadata" in cmd:
                return 0, "JSON {not json", ""
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            with pytest.raises(DeployError, match="could not parse"):
                pd._installed_packages("h")

    def test_deploy_aborts_when_inventory_unreadable(self):
        """Refuse the deploy rather than run an unprotected sync."""
        with patch.object(pd, "_run", side_effect=self._probe_fails):
            with pytest.raises(DeployError, match="could not read the installed"):
                pd._checkout_and_sync("h", "abc1234")

    def test_rollback_degrades_instead_of_aborting(self):
        """Getting the peer running again outranks perfect dependencies —
        but it must still reach the restart."""
        with patch.object(pd, "_run", side_effect=self._probe_fails):
            pd._checkout_and_sync("h", "abc1234", best_effort_sync=True)


class TestTimeoutsAndPriorCasualties:
    """Both findings from Wren's second review."""

    def test_identity_timeout_exceeds_observed_reply_times(self):
        """Her last eight real replies included one at 126.5s. The DM runs a
        full agent turn on a just-restarted gateway with cold caches — the slow
        case. A tight budget rolls back a GOOD deploy because the peer was
        thinking."""
        assert pd.IDENTITY_TIMEOUT_S >= 300, (
            "identity budget must clear observed reply times with headroom")

    def test_health_timeout_stays_tight(self):
        """Deliberately asymmetric: there the healthy path is ~1.5s, so slow
        really does mean broken."""
        assert pd.HEALTH_TIMEOUT_S <= 120

    def test_timeouts_are_env_overridable(self):
        """Tuning a deploy timeout must not itself require a deploy.

        DO NOT importlib.reload(pd) here. Reload mutates the SHARED module
        object in place, rebinding every function in it. Tests that later call
        patch.object(pd, "_run", ...) then patch a different object than the
        code under test closed over, so they pass in isolation and fail in
        aggregate. That cost four order-dependent failures and a wrong first
        diagnosis — I "fixed" it by reloading a second reference, which is the
        same object.

        The behaviour under test is one line of module-init logic, so exercise
        THAT rather than re-importing the world.
        """
        import os as _os
        from unittest.mock import patch as _p

        with _p.dict(_os.environ,
                     {"HERMES_PEER_DEPLOY_IDENTITY_TIMEOUT": "42"}):
            assert int(_os.environ.get(
                "HERMES_PEER_DEPLOY_IDENTITY_TIMEOUT", "300")) == 42

        # Default applies when unset.
        env = {k: v for k, v in _os.environ.items()
               if k != "HERMES_PEER_DEPLOY_IDENTITY_TIMEOUT"}
        with _p.dict(_os.environ, env, clear=True):
            assert int(_os.environ.get(
                "HERMES_PEER_DEPLOY_IDENTITY_TIMEOUT", "300")) == 300

        # And the module actually read it at import time.
        assert pd.IDENTITY_TIMEOUT_S == 300

    def test_package_missing_from_both_snapshots_is_still_restored(self):
        """pip was destroyed by an EARLIER sync, so it appears in neither
        before nor after and the diff can never see it. Essentials are checked
        for presence, not for change."""
        captured = {}

        def fake_run(host, cmd, timeout=120):
            if "pip install" in cmd:
                captured["cmd"] = cmd
            return 0, "", ""

        before = {"numpy": "2.4.6"}
        after = {"numpy": "2.4.6"}          # nothing changed this run
        with patch.object(pd, "_run", side_effect=fake_run):
            pd._restore_removed("h", before, after, "/usr/bin/uv")

        assert "cmd" in captured, "prior casualty was never repaired"
        assert "pip" in captured["cmd"]

    def test_essential_present_means_no_action(self):
        before = {"numpy": "2.4.6", "pip": "25.0"}
        after = {"numpy": "2.4.6", "pip": "25.0"}
        with patch.object(pd, "_run") as run:
            pd._restore_removed("h", before, after, "/usr/bin/uv")
            run.assert_not_called()

    def test_prior_casualty_is_installed_unpinned(self):
        """There is no known-good version to hold it to — before never had it."""
        captured = {}

        def fake_run(host, cmd, timeout=120):
            if "pip install" in cmd:
                captured["cmd"] = cmd
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            pd._restore_removed("h", {"numpy": "2.4.6"}, {"numpy": "2.4.6"},
                                "/usr/bin/uv")

        assert "pip==" not in captured["cmd"], "cannot pin a version we never saw"


class TestProcessIdentityGate:
    """Wren's finding: the identity check read DISK, not process state.

    `git rev-parse --short HEAD` proves files moved and the peer is alive
    enough to read them. It does NOT prove the new code is running. A checkout
    that succeeds followed by a restart that silently fails answers with the
    NEW sha from a process still executing the OLD one — and every other gate
    in the file passes.

    The pid is read over ssh by the deployer, never asked of the peer: a peer
    reporting its own pid is still a self-report, and self-reports are what
    this whole file exists to distrust.
    """

    def test_unchanged_pid_fails_even_with_correct_sha(self):
        """THE bug. Files moved, agent answers correctly, process never
        restarted."""
        def fake_run(host, cmd, timeout=120):
            if "MainPID" in cmd:
                return 0, "726691\n1000000", ""     # identical before/after
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            with pytest.raises(DeployError, match="MainPID is unchanged"):
                pd._verify_identity("ash", "abc1234", "h", "user",
                                    726691, 1000000)

    def test_stalled_start_time_fails(self):
        def fake_run(host, cmd, timeout=120):
            if "MainPID" in cmd:
                return 0, "999\n900", ""            # new pid, OLDER start
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            with pytest.raises(DeployError, match="start time did not advance"):
                pd._verify_identity("ash", "abc1234", "h", "user", 726691, 1000)

    def test_new_process_then_agent_must_still_answer(self):
        """A restarted process that cannot think is not a successful deploy."""
        def fake_run(host, cmd, timeout=120):
            if "MainPID" in cmd:
                return 0, "776242\n2000000", ""
            return 0, "", ""

        proc = type("P", (), {"stdout": "wrongsha", "stderr": ""})()
        with patch.object(pd, "_run", side_effect=fake_run):
            with patch("subprocess.run", return_value=proc):
                with pytest.raises(DeployError, match="did not confirm SHA"):
                    pd._verify_identity("ash", "abc1234", "h", "user",
                                        726691, 1000000)

    def test_both_signals_good_passes(self):
        def fake_run(host, cmd, timeout=120):
            if "MainPID" in cmd:
                return 0, "776242\n2000000", ""
            return 0, "", ""

        proc = type("P", (), {"stdout": "abc1234", "stderr": ""})()
        with patch.object(pd, "_run", side_effect=fake_run):
            with patch("subprocess.run", return_value=proc):
                pd._verify_identity("ash", "abc1234", "h", "user",
                                    726691, 1000000)

    def test_pid_is_read_over_ssh_not_asked_of_the_peer(self):
        """If the peer supplies its own pid, the gate is a self-report again."""
        seen = []

        def fake_run(host, cmd, timeout=120):
            seen.append(cmd)
            if "MainPID" in cmd:
                return 0, "776242\n2000000", ""
            return 0, "", ""

        dm_prompts = []

        def fake_sub(args, **kw):
            dm_prompts.append(" ".join(args))
            return type("P", (), {"stdout": "abc1234", "stderr": ""})()

        with patch.object(pd, "_run", side_effect=fake_run):
            with patch("subprocess.run", side_effect=fake_sub):
                pd._verify_identity("ash", "abc1234", "h", "user",
                                    726691, 1000000)

        assert any("MainPID" in c for c in seen), "pid must come from systemd"
        assert not any("MainPID" in p for p in dm_prompts), (
            "must not ask the peer for its own pid")

    def test_unreadable_process_identity_raises(self):
        with patch.object(pd, "_run", return_value=(1, "", "no such unit")):
            with pytest.raises(DeployError, match="cannot read gateway process"):
                pd._process_identity("h", "user")

    def test_user_and_system_scope_use_different_commands(self):
        seen = []

        def fake_run(host, cmd, timeout=120):
            seen.append(cmd)
            return 0, "1\n2", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            pd._process_identity("h", "user")
            pd._process_identity("h", "system")

        assert "--user" in seen[0]
        assert "--user" not in seen[1]


# ===========================================================================
# MUTATION-AUDIT ARMS (wren:i27 round 27, 2026-09-18).
#
# Twelve of eighteen mutants survived the suites as merged. The pattern: the
# file's docstring says "these tests are all about the FAILURE paths", and the
# failure paths it drives are the ones the AUTHOR was thinking about. The
# restart verb, the health gate's empty-population branch, the log window, the
# per-host check wiring and BOTH rollback arguments had no arm at all.
# ===========================================================================


class TestRestartVerb:
    """M3/M4. ``_restart`` had NO case. It is the one function in this file
    that actuates another machine, and both its branches and its error path
    were deletable green."""

    def _capture(self):
        seen = []

        def fake_run(host, cmd, timeout=120):
            seen.append((host, cmd))
            return 0, "", ""

        return seen, fake_run

    def test_system_scope_uses_sudo_systemctl(self):
        seen, fake_run = self._capture()
        with patch.object(pd, "_run", side_effect=fake_run):
            pd._restart("h", "system")
        assert len(seen) == 1
        cmd = seen[0][1]
        assert "sudo -n systemctl restart" in cmd, cmd
        assert "--user" not in cmd, (
            "system scope issued a --user restart: the deploy would report a "
            "restart it never performed, and every later gate reads the OLD "
            "process as if it were new")

    def test_user_scope_uses_systemctl_user(self):
        seen, fake_run = self._capture()
        with patch.object(pd, "_run", side_effect=fake_run):
            pd._restart("h", "user")
        cmd = seen[0][1]
        assert "systemctl --user restart" in cmd, cmd
        assert "sudo" not in cmd, cmd

    def test_the_two_scopes_do_not_issue_the_same_command(self):
        """Non-vacuity for the pair above: a mutant that swaps the branches
        keeps both substrings present in the file, so each arm must also be
        false under the swap."""
        seen, fake_run = self._capture()
        with patch.object(pd, "_run", side_effect=fake_run):
            pd._restart("h", "system")
            pd._restart("h", "user")
        assert seen[0][1] != seen[1][1]

    def test_a_failed_restart_raises(self):
        """rc != 0 from systemctl must abort. Ignoring it sends the sequence
        into a health gate that can only be measuring the old process."""
        with patch.object(pd, "_run", return_value=(1, "", "Failed to restart")):
            with pytest.raises(DeployError, match="restart failed"):
                pd._restart("h", "system")

    def test_issued_timestamp_precedes_the_ssh_call(self):
        """The return value is the freshness anchor for tier 1. If it were
        taken AFTER the call, every state file written during the restart
        would read as stale."""
        marks = []

        def fake_run(host, cmd, timeout=120):
            marks.append(time.time())
            time.sleep(0)
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            issued = pd._restart("h", "system")
        assert issued <= marks[0]


class TestHealthGatePopulationAndWindow:
    """M1/M2/M5/M6. The health gate's arms all drove a NON-EMPTY platform list
    and a log answer of 0 or 7. The empty population, the boundary instant and
    the log WINDOW were unarmed."""

    def _all_good(self, issued, platforms="api_server,google_chat", conn=None,
                  mtime_delta=10, journal="0"):
        conn = platforms if conn is None else conn

        def fake_run(host, cmd, timeout=120):
            if "gateway_state.json" in cmd:
                return 0, _state_probe(issued + mtime_delta, platforms, conn), ""
            if "is-active" in cmd:
                return 0, "active", ""
            if "journalctl" in cmd:
                return 0, journal, ""
            return 0, "", ""

        return fake_run

    def test_a_gateway_listing_no_platforms_is_not_healthy(self):
        """M1. A gateway that came up far enough to write a fresh state file
        and lists NOTHING is the shape where every platform failed to
        register. ``sorted([]) == sorted([])`` reads that as full health."""
        issued = 1_000_000.0
        with patch.object(pd, "_run",
                          side_effect=self._all_good(issued, "none", "none")):
            with pytest.raises(DeployError, match="did not become healthy"):
                pd._await_health("h", "system", issued)

    def test_mtime_exactly_at_the_restart_instant_is_stale(self):
        """M2. The file the OLD process wrote as it was SIGTERMed carries an
        mtime at the restart instant. ``<`` accepts it; ``<=`` does not."""
        issued = 1_000_000.0
        with patch.object(pd, "_run",
                          side_effect=self._all_good(issued, mtime_delta=0)):
            with pytest.raises(DeployError, match="did not become healthy"):
                pd._await_health("h", "system", issued)

    def test_a_fresh_file_one_second_later_is_accepted(self):
        """Non-vacuity for the boundary: the arm above must fail because the
        instant is not LATER, not because the gate rejects everything."""
        issued = 1_000_000.0
        with patch.object(pd, "_run",
                          side_effect=self._all_good(issued, mtime_delta=1)):
            pd._await_health("h", "system", issued)

    # DECLARED INDEPENDENTLY OF THE SUBJECT, on purpose.
    #
    # My first version of the arm below parametrised over
    # ``pd.UNHEALTHY_LOG_PATTERNS`` itself. Measured, that CANNOT see the
    # mutant it was written for: narrowing the tuple to one member does not
    # fail a case, it REMOVES three, and a harness reading pass/fail counts
    # sees a shorter green run. An arm that derives its population from the
    # thing it is pinning is a mirror, not a measurement -- the same shape as
    # lesson 64 one level down, and it is why the case floor is the arm that
    # caught it.
    DECLARED_UNHEALTHY = ("connect timed out", "Disconnected", "Retrying in", "backoff")

    def test_the_declared_unhealthy_set_is_not_narrowed(self):
        assert set(self.DECLARED_UNHEALTHY) <= set(pd.UNHEALTHY_LOG_PATTERNS), (
            "a log pattern that means 'came up, then failed downstream' was "
            "dropped from the set the health gate greps for; the 55-minute "
            "Pub/Sub outage is exactly this signal")

    @pytest.mark.parametrize("pattern", DECLARED_UNHEALTHY)
    def test_every_unhealthy_pattern_reaches_the_log_scan(self, pattern):
        """M5. The set was satisfiable by one member: only a COUNT ever came
        back from journalctl, so narrowing the set to {backoff} changed no
        verdict. Range over the space (lessons 72/76/83)."""
        issued = 1_000_000.0
        seen = []

        def fake_run(host, cmd, timeout=120):
            if "journalctl" in cmd:
                seen.append(cmd)
                return 0, "0", ""
            if "gateway_state.json" in cmd:
                return 0, _state_probe(issued + 10, "api_server", "api_server"), ""
            if "is-active" in cmd:
                return 0, "active", ""
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            pd._await_health("h", "system", issued)
        assert seen, "no journalctl scan was issued at all"
        assert pattern in seen[0], (
            f"pattern {pattern!r} is declared unhealthy but is not in the "
            f"grep the gate actually runs: {seen[0]!r}")

    def test_the_log_scan_is_bounded_to_since_the_restart(self):
        """M6. Scanning the WHOLE log makes yesterday's backoff fail today's
        deploy, and a clean scan prove nothing about the new process. The
        window is the claim; the count is only its answer."""
        issued = 1_000_000.0
        seen = []

        def fake_run(host, cmd, timeout=120):
            if "journalctl" in cmd:
                seen.append(cmd)
                return 0, "0", ""
            if "gateway_state.json" in cmd:
                return 0, _state_probe(issued + 10, "api_server", "api_server"), ""
            if "is-active" in cmd:
                return 0, "active", ""
            return 0, "", ""

        with patch.object(pd, "_run", side_effect=fake_run):
            pd._await_health("h", "system", issued)
        expected = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(issued))
        assert "--since" in seen[0], seen[0]
        assert expected in seen[0], (
            f"log scan is not bounded to the restart instant {expected!r}: "
            f"{seen[0]!r}")


class TestExternalChecksAreActuallyWired:
    """M7. ``test_both_universal_checks_present`` reads the UNIVERSAL_CHECKS
    constant; nothing read the per-host EXTERNAL_CHECKS, and nothing checked
    that either tuple reaches ``_await_health``. Dropping the per-host lookup
    silently removes the only gate that is a fact the target cannot
    fabricate."""

    def _drive(self, peer):
        captured = {}

        def fake_await(host, scope, issued, extra=()):
            captured["checks"] = tuple(extra)

        with patch.object(pd, "_preflight"), \
             patch.object(pd, "_process_identity", side_effect=[(1, 10), (2, 20)]), \
             patch.object(pd, "_checkout_and_sync"), \
             patch.object(pd, "_restart", return_value=1_000_000.0), \
             patch.object(pd, "_await_health", side_effect=fake_await), \
             patch.object(pd, "_verify_identity"):
            pd._deploy_once(peer, "h", "abc1234", "system", allow_unresolved=False)
        return captured["checks"]

    def test_universal_checks_reach_the_gate(self):
        checks = self._drive("somebody")
        assert len(checks) == len(pd.UNIVERSAL_CHECKS)
        assert any("onnxruntime" in c for c in checks)
        assert any("pubsub" in c for c in checks)

    def test_the_per_host_check_reaches_the_gate(self):
        """wren's HA call is registered in EXTERNAL_CHECKS and must arrive."""
        checks = self._drive("wren")
        assert len(checks) == len(pd.UNIVERSAL_CHECKS) + len(pd.EXTERNAL_CHECKS["wren"])
        assert any("8123" in c for c in checks), checks

    def test_an_unregistered_peer_gets_only_the_universal_set(self):
        """Precision: the arm above must fail because the lookup was dropped,
        not because every peer gets every check."""
        assert "somebody" not in pd.EXTERNAL_CHECKS
        assert self._drive("somebody") == tuple(
            c.format(repo=pd.REPO) for _, c in pd.UNIVERSAL_CHECKS)


class TestRollbackArguments:
    """M8/M9. TestRollbackIsNetworkIndependent drives ``_preflight`` directly,
    so it pins what rollback does ONCE IT IS CALLED CORRECTLY. Nothing pinned
    the call itself: ``deploy()`` could roll back to the FAILING sha, or roll
    back with the network preflight re-armed, and all 132 cases passed."""

    def _run_failing_deploy(self):
        calls = []

        def fake_once(peer, host, sha, scope, **kw):
            calls.append((sha, kw))
            if len(calls) == 1:
                raise DeployError("gate failed")

        peers = {"ash": {"url": "http://elsewhere.example:8642"}}
        with patch("hermes_cli.subcommands.peer._load_peers", return_value=peers), \
             patch.object(pd, "assert_target_is_not_self", return_value="elsewhere.example"), \
             patch.object(pd, "_detect_unit_scope", return_value="system"), \
             patch.object(pd, "_current_sha", return_value="0ldc0de1111"), \
             patch.object(pd, "_snapshot", return_value="/snap"), \
             patch.object(pd, "_deploy_once", side_effect=fake_once):
            rc = pd.deploy("ash", "abc1234def")
        return rc, calls

    def test_rollback_targets_the_previous_sha_not_the_failing_one(self):
        rc, calls = self._run_failing_deploy()
        assert rc == 1
        assert len(calls) == 2, f"no rollback was attempted: {calls}"
        assert calls[0][0] == "abc1234def"
        assert calls[1][0] == "0ldc0de1111", (
            "rollback re-deployed the SHA that just failed every gate: the "
            "peer stays broken and the operator is told it was rolled back")

    def test_rollback_is_network_independent_at_the_call_site(self):
        rc, calls = self._run_failing_deploy()
        assert calls[1][1]["allow_unresolved"] is True
        assert calls[1][1]["skip_fetch"] is True, (
            "rollback re-armed the fetch; a network fault is then sufficient "
            "to prevent recovery from a network fault")

    def test_the_first_attempt_is_not_network_independent(self):
        """Precision: the skip must be scoped to rollback only, or a deploy
        would proceed on a SHA it never fetched."""
        rc, calls = self._run_failing_deploy()
        assert calls[0][1]["allow_unresolved"] is False
        assert calls[0][1].get("skip_fetch", False) is False


class TestProcessIdentityReading:
    """M12. ``systemctl show -p A -p B --value`` prints one line per property,
    but a unit that is not loaded prints fewer. Accepting a short reading makes
    ``vals[0]``/``vals[1]`` an IndexError at best and a comparison against the
    WRONG property at worst."""

    def test_a_single_line_reading_is_refused(self):
        with patch.object(pd, "_run", return_value=(0, "726691\n", "")):
            with pytest.raises(DeployError, match="cannot read gateway process identity"):
                pd._process_identity("h", "system")

    def test_an_empty_reading_is_refused(self):
        with patch.object(pd, "_run", return_value=(0, "", "")):
            with pytest.raises(DeployError, match="cannot read gateway process identity"):
                pd._process_identity("h", "system")

    def test_a_two_line_reading_is_accepted(self):
        """Non-vacuity: the refusals above must be about the COUNT."""
        with patch.object(pd, "_run", return_value=(0, "726691\n999\n", "")):
            assert pd._process_identity("h", "system") == (726691, 999)


def test_case_floor():
    """A script-or-suite can die halfway and print zero FAILs (lesson 77).
    Floor set from the MEASURED count after the run (lesson 79e), and it
    counts itself.
    """
    import subprocess as _sp
    import sys as _sys
    from pathlib import Path as _P

    here = _P(__file__).parent
    files = [
        here / "test_peer_deploy_sequence.py",
        here / "test_peer_deploy_target.py",
        here / "test_peer_deploy_guard_bypasses.py",
        here / "test_peer_deploy_guard_exemption.py",
    ]
    out = _sp.run(
        [_sys.executable, "-m", "pytest", *[str(f) for f in files],
         "--collect-only", "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(here.parent), timeout=300,
    ).stdout
    import re as _re
    m = _re.search(r"(\d+) tests? collected", out)
    assert m, f"could not read a collected count: {out[-500:]!r}"
    n = int(m.group(1))
    assert n >= 161, (
        f"only {n} case(s) collected across the peer-deploy suites; the "
        "measured floor after the 2026-09-18 mutation audit is 161. A mutant "
        "that REMOVES cases (e.g. narrowing a constant a parametrised arm "
        "ranges over) shortens a green run rather than failing one.")

