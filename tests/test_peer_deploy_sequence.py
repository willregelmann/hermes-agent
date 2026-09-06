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

    def test_timeouts_are_env_overridable(self, monkeypatch):
        """Tuning a deploy timeout must not itself require a deploy."""
        import importlib
        monkeypatch.setenv("HERMES_PEER_DEPLOY_IDENTITY_TIMEOUT", "42")
        reloaded = importlib.reload(pd)
        try:
            assert reloaded.IDENTITY_TIMEOUT_S == 42
        finally:
            monkeypatch.delenv("HERMES_PEER_DEPLOY_IDENTITY_TIMEOUT")
            importlib.reload(pd)

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
