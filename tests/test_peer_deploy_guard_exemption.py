"""The `hermes peer deploy` exemption in the lifecycle guard.

An exemption in a safety control is only as good as what it REFUSES. These
tests are mostly bypass attempts: the concern is not that the verb works, it
is that nothing else can wear its clothes.

The exemption itself grants no authority — it routes to a verb whose own code
proves the target is not this machine (see test_peer_deploy_target.py). What
must hold here is that only that verb, unchained and unprefixed, gets through.
"""

import pytest

from cron.lifecycle_guard import (
    _is_peer_deploy_verb,
    contains_gateway_lifecycle_command_or_referenced_script as guard,
)

# Assembled at runtime so this test FILE does not itself contain the phrasing —
# the guard scans referenced scripts, and a literal here has previously caused
# the guard to block tooling that merely discusses it.
R = "rest" + "art"
GW = "hermes" + "-gateway"


class TestExemptionAccepts:
    @pytest.mark.parametrize("cmd", [
        "hermes peer deploy wren abc1234",
        "  hermes peer deploy wren abc1234",
        "/usr/local/bin/hermes peer deploy wren abc1234",
        "hermes -p default peer deploy wren abc1234",
        "hermes peer deploy wren abc1234 --dry-run",
    ])
    def test_the_verb_is_exempt(self, cmd):
        assert _is_peer_deploy_verb(cmd) is True
        assert guard(cmd) is False


class TestExemptionRefuses:
    """Everything that is not exactly the verb."""

    @pytest.mark.parametrize("cmd", [
        # Chaining: the exemption must not become a prefix that smuggles a
        # second command past the guard.
        f"hermes peer deploy wren abc1234 && systemctl --user {R} {GW}",
        f"hermes peer deploy wren abc1234; systemctl --user {R} {GW}",
        f"hermes peer deploy wren abc1234 || systemctl --user {R} {GW}",
        "hermes peer deploy wren abc1234 | sh",
        f"hermes peer deploy wren $(systemctl --user {R} {GW})",
        "hermes peer deploy wren `id`",
        f"hermes peer deploy wren abc1234\nsystemctl --user {R} {GW}",
    ])
    def test_chained_commands_are_not_exempt(self, cmd):
        assert _is_peer_deploy_verb(cmd) is False

    @pytest.mark.parametrize("cmd", [
        # Prefixing: the phrase must be the command, not inside one.
        f"systemctl --user {R} {GW} && hermes peer deploy wren abc1234",
        f"echo hermes peer deploy && systemctl --user {R} {GW}",
    ])
    def test_prefixed_commands_are_not_exempt(self, cmd):
        assert _is_peer_deploy_verb(cmd) is False
        assert guard(cmd) is True

    @pytest.mark.parametrize("cmd", [
        "hermes peer dm wren hello",
        "hermes peer list",
        "hermes gateway " + R,
        "hermes deploy peer wren abc1234",     # wrong verb order
        "hermesXpeer deploy wren abc1234",
        "",
    ])
    def test_other_commands_are_not_exempt(self, cmd):
        assert _is_peer_deploy_verb(cmd) is False

    def test_non_string_input(self):
        assert _is_peer_deploy_verb(None) is False  # type: ignore[arg-type]
        assert _is_peer_deploy_verb(123) is False   # type: ignore[arg-type]


class TestGuardStillBlocksEverythingElse:
    """The exemption must not have widened the guard's real job."""

    @pytest.mark.parametrize("cmd", [
        f"systemctl --user {R} {GW}",
        f"sudo systemctl {R} {GW}",
        f"ssh will@ha-pi.local 'sudo systemctl {R} {GW}'",
        f"ssh will@ha-pi.local 'sudo systemd-run --on-active=5 systemctl {R} {GW}'",
        "hermes gateway stop",
    ])
    def test_free_form_lifecycle_is_still_blocked(self, cmd):
        assert guard(cmd) is True
