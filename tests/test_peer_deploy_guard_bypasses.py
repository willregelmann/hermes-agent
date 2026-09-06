"""Bypass attempts against the peer-deploy guard exemption.

Every case here was found by Wren in adversarial review, or by re-running the
suite after a fix that turned out to open a different hole. They are regression
tests for real defects, not hypotheticals.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cron.lifecycle_guard import (  # noqa: E402
    _is_peer_deploy_verb,
    contains_gateway_lifecycle_command_or_referenced_script as guard,
)

VERB = "hermes peer deploy wren abc123"
LIFECYCLE = "systemctl --user restart hermes-gateway"


class TestExemptionAllowsTheSanctionedVerb:
    @pytest.mark.parametrize("cmd", [
        VERB,
        f"{VERB} --dry-run",
        f"{VERB} --rollback-to def456",
        f"/home/will/.local/bin/{VERB}",
    ])
    def test_clean_verb_is_exempt_and_unblocked(self, cmd):
        assert _is_peer_deploy_verb(cmd) is True
        assert not guard(cmd)


class TestSeparatorBypasses:
    """A blocklist of separators is a game you lose once, and lose silently."""

    @pytest.mark.parametrize("sep,label", [
        (" && ", "and-chain"),
        ("; ", "semicolon"),
        (" | ", "pipe"),
        (" || ", "or-chain"),
        # WREN'S FIND: `&&` was on the old blocklist, a lone `&` was not.
        # One ampersand backgrounds the deploy and runs the next command.
        (" & ", "single-ampersand-backgrounding"),
    ])
    def test_chained_lifecycle_is_not_exempt_and_is_blocked(self, sep, label):
        cmd = f"{VERB}{sep}{LIFECYCLE}"
        assert _is_peer_deploy_verb(cmd) is False, label
        assert guard(cmd), label


class TestLineTerminatorBypasses:
    """shlex.split treats these as ordinary whitespace.

    Tokenising ALONE made the newline case WORSE: it split a two-command string
    into clean word tokens that all passed the allow-list. The explicit
    line-terminator check has to come first. Neither check suffices alone.
    """

    @pytest.mark.parametrize("ch,label", [
        ("\n", "newline"),
        ("\r", "carriage-return"),      # WREN'S FIND: ^ without MULTILINE
        ("\v", "vertical-tab"),         # stops \n, does not stop \r
        ("\f", "form-feed"),
        ("\x00", "nul"),
        ("\u2028", "unicode-line-sep"),
        ("\u2029", "unicode-para-sep"),
    ])
    def test_terminator_joined_lifecycle_is_not_exempt_and_is_blocked(self, ch, label):
        cmd = f"{VERB}{ch}{LIFECYCLE}"
        assert _is_peer_deploy_verb(cmd) is False, label
        assert guard(cmd), label


class TestSubstitutionBypasses:
    @pytest.mark.parametrize("cmd", [
        f"{VERB} $({LIFECYCLE})",
        f"{VERB} `{LIFECYCLE}`",
        f'{VERB} "; {LIFECYCLE}"',
    ])
    def test_substitution_is_not_exempt_and_is_blocked(self, cmd):
        assert _is_peer_deploy_verb(cmd) is False
        assert guard(cmd)


class TestExemptionFailsClosedOnOddInput:
    """These carry no lifecycle command, so the guard has nothing to block.

    What matters is that the EXEMPTION refuses them: it must never widen to
    input it cannot fully parse.
    """

    @pytest.mark.parametrize("cmd", [
        f'{VERB} "unclosed',       # shlex raises ValueError
        f"{VERB} > /tmp/x",        # redirect
        f"{VERB} 2>&1",
        "",
        "   ",
    ])
    def test_not_exempt(self, cmd):
        assert _is_peer_deploy_verb(cmd) is False

    def test_non_string_input(self):
        assert _is_peer_deploy_verb(None) is False  # type: ignore[arg-type]
        assert _is_peer_deploy_verb(123) is False  # type: ignore[arg-type]


class TestFreeFormLifecycleStaysBlocked:
    @pytest.mark.parametrize("cmd", [
        LIFECYCLE,
        "systemctl restart hermes-gateway",
        "hermes gateway restart",
        "sudo systemctl restart hermes-gateway",
        "systemd-run --user systemctl restart hermes-gateway",
    ])
    def test_blocked(self, cmd):
        assert _is_peer_deploy_verb(cmd) is False
        assert guard(cmd)


class TestRegexIsNotTheGuarantee:
    """The raw regex matches a PREFIX; only the full check is the guard.

    A bare _PEER_DEPLOY_RE.match() succeeds on a multi-line string, which looks
    like a bypass if you test the regex instead of the entry point. It is not
    exploitable through _is_peer_deploy_verb, but anyone reusing the pattern
    elsewhere would inherit the foot-gun.
    """

    def test_raw_regex_matches_prefix_but_verb_check_refuses(self):
        from cron.lifecycle_guard import _PEER_DEPLOY_RE
        cmd = f"{VERB}\n{LIFECYCLE}"
        assert _PEER_DEPLOY_RE.match(cmd) is not None
        assert _is_peer_deploy_verb(cmd) is False
        assert guard(cmd)
