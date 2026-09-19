"""Target-aware gateway lifecycle guard: a peer restart is not a self-restart (PR #24).

WHY: the guard blocks any command whose CONTENT looks like a gateway restart.
Its stated rationale is that the gateway SIGTERMs its own child mid-restart —
true locally, false for a different machine. Two agents restarting each other
over ssh is a sanctioned backdoor (the primary path is ``restart_peer_gateway``,
#54) and it was blocked by a guard whose reason did not apply.

THE DANGEROUS DIRECTION IS THE EXEMPTION, so every loosening case here is
paired with a case proving the thing it must still block. A guard that stops
guarding is worse than one that over-blocks.
"""

from __future__ import annotations

import socket

import pytest

from cron.lifecycle_guard import _ssh_target_is_remote
from cron.lifecycle_guard import contains_gateway_lifecycle_command as blocked

HOST = socket.gethostname().split(".", 1)[0]

#  Lesson (ash862, PR #24 review): a fixture that hardcodes a peer NAME is
#  silently host-bound. The predicate under test derives remoteness from
#  socket.gethostname() on whichever box runs it, so a literal peer name is
#  a peer on one box and is SELF on the other -- and every exemption case
#  then reads as refused against a perfectly correct guard.
#  Two destinations, deliberately distinct:
#    SYNTH -- constructed FROM this host, so it can never equal it. Used for
#             the exemption-positive cases: host-independent by construction.
#    PEER  -- a real box that is not this one, so one case still pins the
#             actual name used in production.
REAL_BOXES = ("ha-pi", "will-MS-7B93")
PEER = next(b for b in REAL_BOXES if b.casefold() != HOST.casefold()) + ".local"
SYNTH = f"not-{HOST}-peer.local"
SSH = ("ssh -F /dev/null -o BatchMode=yes -o ConnectTimeout=8 "
       f"-i /home/will/.ssh/id_ed25519 will@{SYNTH}")
LOCAL = "systemctl --user restart hermes-gateway"
HVERB = "hermes gateway restart"


def test_fixture_destinations_are_not_this_host():
    """Non-vacuity: if either destination were this host, every ALLOW case would be meaningless."""
    assert SYNTH.split(".", 1)[0].casefold() != HOST.casefold()
    assert PEER.split(".", 1)[0].casefold() != HOST.casefold()


# The guard's actual job.
@pytest.mark.parametrize("cmd", [
    pytest.param(LOCAL, id="local-systemctl"),
    pytest.param(HVERB, id="local-hermes-verb"),
    pytest.param(f"ssh {HOST} {LOCAL}", id="ssh-own-hostname"),
    pytest.param(f"ssh {HOST}.local {LOCAL}", id="ssh-own-mdns"),
    pytest.param(f"ssh will@{HOST}.local {LOCAL}", id="ssh-user-at-own-host"),
    pytest.param(f'ssh "$TARGET" {LOCAL}', id="variable-destination"),
    pytest.param(f"ssh `cat host.txt` {LOCAL}", id="backtick-destination"),
    pytest.param(f"myssh {SYNTH} {LOCAL}", id="ssh-lookalike-leader"),
    pytest.param(f"{SYNTH}: {LOCAL}", id="remote-looking-prose-without-ssh"),
])
def test_self_directed_lifecycle_stays_blocked(cmd):
    assert blocked(cmd) is True


# The peer-restart backdoor.
@pytest.mark.parametrize("cmd", [
    pytest.param(f"ssh {SYNTH} {LOCAL}", id="plain-ssh"),
    pytest.param(f"ssh will@{SYNTH} {LOCAL}", id="user-at-peer"),
    pytest.param(f"{SSH} '{LOCAL}'", id="real-option-soup"),
    pytest.param(f"/usr/bin/ssh {SYNTH} {LOCAL}", id="absolute-ssh-path"),
    pytest.param(f"ssh {PEER} {HVERB}", id="real-peer-hermes-verb"),
    # Chaining alone is not a block: every segment here is a remote ssh.
    pytest.param(f"ssh {SYNTH} uptime; ssh {SYNTH} {HVERB}", id="two-remote-segments"),
])
def test_restart_of_another_host_is_allowed(cmd):
    assert blocked(cmd) is False


#  The exemption is all(), not any(): a command is exempt only when EVERY
#  executable segment is a provably-remote ssh. PR #24 claimed this in prose
#  and nothing asserted it -- mutating all()->any() survived all 25 cases,
#  so the shell operator that makes the exemption safe was uncovered.
@pytest.mark.parametrize("cmd", [
    pytest.param(f"ssh {SYNTH} uptime; {LOCAL}", id="remote-then-local"),
    pytest.param(f"{LOCAL}; ssh {SYNTH} uptime", id="local-then-remote"),
    pytest.param(f"ssh {SYNTH} true && {HVERB}", id="and-chain"),
    pytest.param(f"ssh {SYNTH} true | {LOCAL}", id="pipe-chain"),
])
def test_one_local_segment_blocks_the_whole_command(cmd):
    assert blocked(cmd) is True


#  _local_host_identities() carries four loopback literals. Only two were
#  asserted; dropping ::1 and 0.0.0.0 survived the whole suite, so half the
#  self-identity set was uncovered.
@pytest.mark.parametrize("spelling", ["localhost", "127.0.0.1", "::1", "0.0.0.0"])
def test_every_loopback_spelling_is_self(spelling):
    assert blocked(f"ssh {spelling} {LOCAL}") is True
    assert _ssh_target_is_remote(["ssh", spelling, "x"]) is False


# The predicate itself: the ALLOW cases above must be able to go red if it were deleted.
@pytest.mark.parametrize("segment, remote", [
    pytest.param(["ssh", SYNTH, "systemctl"], True, id="peer-host"),
    pytest.param(["ssh", "localhost", "systemctl"], False, id="localhost"),
    pytest.param(["ssh", HOST, "systemctl"], False, id="own-hostname"),
    pytest.param([], False, id="empty-segment"),
    pytest.param(["scp", SYNTH, "x"], False, id="non-ssh-leader"),
    # Option VALUES are skipped when finding the destination...
    pytest.param(["ssh", "-i", "/home/will/.ssh/id_ed25519", "-o", "BatchMode=yes", SYNTH, "systemctl"],
                 True, id="option-values-skipped"),
    # ...and never mistaken FOR it, or localhost would never be checked.
    pytest.param(["ssh", "-F", "/dev/null", "localhost", "x"], False, id="option-value-not-a-host"),
])
def test_ssh_target_is_remote_fails_closed(segment, remote):
    assert _ssh_target_is_remote(segment) is remote
