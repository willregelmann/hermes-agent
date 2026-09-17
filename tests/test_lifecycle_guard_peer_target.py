"""Target-aware gateway lifecycle guard: a peer restart is not a self-restart.

WHY: the guard blocks any command whose CONTENT looks like a gateway restart.
Its stated rationale is that the gateway SIGTERMs its own child mid-restart —
true locally, false for a different machine. Two agents restarting each other
over ssh is the sanctioned mutual-deploy path and it was blocked by a guard
whose reason did not apply.

THE DANGEROUS DIRECTION IS THE EXEMPTION, so every loosening case here is
paired with a case proving the thing it must still block. A guard that stops
guarding is worse than one that over-blocks.

Run: python3 tests/test_lifecycle_guard_peer_target.py
"""
from __future__ import annotations

import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cron.lifecycle_guard import contains_gateway_lifecycle_command as blocked  # noqa: E402
from cron.lifecycle_guard import _ssh_target_is_remote as _ssh_target_is_remote_late  # noqa: E402

fails: list = []
ran = 0


def case(name, ok, detail=""):
    global ran
    ran += 1
    if ok:
        print(f"  PASS  {name}")
    else:
        fails.append(name)
        print(f"  FAIL  {name}  {detail}")


HOST = socket.gethostname().split(".", 1)[0]

#  Lesson (ash862, PR #24 review): a fixture that hardcodes a peer NAME is
#  silently host-bound. The predicate under test derives remoteness from
#  socket.gethostname() on whichever box runs it, so a literal peer name is
#  a peer on one box and is SELF on the other -- and every exemption case
#  then reads as refused against a perfectly correct guard.
#  Two destinations, deliberately distinct:
#    SYNTH -- constructed FROM this host, so it can never equal it. Used for
#             the exemption-positive cases: host-independent by construction.
#    PEER  -- the real other box, resolved per-host, so one case still pins
#             the actual name used in production.
REAL_BOXES = ("ha-pi", "will-MS-7B93")
_others = [b for b in REAL_BOXES if b != HOST]
PEER = f"{_others[0]}.local"
SYNTH = f"not-{HOST}-peer.local"
SSH = ("ssh -F /dev/null -o BatchMode=yes -o ConnectTimeout=8 "
       f"-i /home/will/.ssh/id_ed25519 will@{SYNTH}")

print("=== MUST STILL BLOCK (the guard's actual job) ===")
case("local systemctl restart is blocked",
     blocked("systemctl --user restart hermes-gateway") is True)
case("local hermes gateway restart is blocked",
     blocked("hermes gateway restart") is True)
case("ssh to LOCALHOST is blocked (transport is not a loophole)",
     blocked("ssh localhost systemctl --user restart hermes-gateway") is True)
case("ssh to 127.0.0.1 is blocked",
     blocked("ssh 127.0.0.1 systemctl --user restart hermes-gateway") is True)
case("ssh to OWN hostname is blocked",
     blocked(f"ssh {HOST} systemctl --user restart hermes-gateway") is True)
case("ssh to own <hostname>.local is blocked",
     blocked(f"ssh {HOST}.local systemctl --user restart hermes-gateway") is True)
case("ssh with user@ to own host is blocked",
     blocked(f"ssh will@{HOST}.local systemctl --user restart hermes-gateway") is True)
case("VARIABLE destination is blocked (unknowable at scan time)",
     blocked('ssh "$TARGET" systemctl --user restart hermes-gateway') is True)
case("backtick destination is blocked",
     blocked("ssh `cat host.txt` systemctl --user restart hermes-gateway") is True)
case("ssh-lookalike leader is blocked (myssh is not ssh)",
     blocked(f"myssh {SYNTH} systemctl --user restart hermes-gateway") is True)
case("no-ssh remote-looking prose is still blocked",
     blocked(f"{SYNTH}: systemctl --user restart hermes-gateway") is True)

print()
print("=== MUST NOW ALLOW (the peer-restart path) ===")
case("plain ssh to a PEER host is allowed",
     blocked(f"ssh {SYNTH} systemctl --user restart hermes-gateway") is False,
     "still blocked")
case("ssh with user@peer is allowed",
     blocked(f"ssh will@{SYNTH} systemctl --user restart hermes-gateway") is False,
     "still blocked")
case("ssh with the real option soup I actually use is allowed",
     blocked(f"{SSH} 'systemctl --user restart hermes-gateway'") is False,
     "still blocked")
case("absolute-path ssh binary is allowed",
     blocked(f"/usr/bin/ssh {SYNTH} systemctl --user restart hermes-gateway") is False,
     "still blocked")
case("peer hermes gateway restart is allowed",
     blocked(f"ssh {PEER} hermes gateway restart") is False,
     "still blocked")

print()
print("=== CHAINED COMMANDS: one local segment poisons the whole command ===")
#  The exemption is all(), not any(): a command is exempt only when EVERY
#  executable segment is a provably-remote ssh. PR #24 claims this in prose
#  and nothing asserted it -- mutating all()->any() survived all 25 cases,
#  so the shell operator that makes the exemption safe was uncovered.
LOCAL = 'systemctl --user restart hermes-gateway'
HVERB = 'hermes gateway restart'
case("remote ssh THEN local restart is blocked",
     blocked(f"ssh {SYNTH} uptime; " + LOCAL) is True,
     "a local lifecycle segment rode in behind a remote one")
case("local restart THEN remote ssh is blocked",
     blocked(LOCAL + f"; ssh {SYNTH} uptime") is True,
     "a local lifecycle segment rode in ahead of a remote one")
case("remote ssh && local hermes verb is blocked",
     blocked(f"ssh {SYNTH} true && " + HVERB) is True,
     "&& chaining bypassed the guard")
case("remote ssh | local restart is blocked",
     blocked(f"ssh {SYNTH} true | " + LOCAL) is True,
     "pipe chaining bypassed the guard")
case("two remote ssh segments stay allowed (chaining is not itself a block)",
     blocked(f"ssh {SYNTH} uptime; ssh {SYNTH} " + HVERB) is False,
     "the chained-command arms are only asserting that chaining blocks")

print()
print("=== LOOPBACK SPELLINGS: every self-name must refuse ===")
#  _local_host_identities() carries four loopback literals. Only two were
#  asserted; dropping ::1 and 0.0.0.0 survived the whole suite, so half the
#  self-identity set was uncovered.
for spelling in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
    case(f"ssh {spelling} self-restart is blocked",
         blocked(f"ssh {spelling} " + LOCAL) is True,
         "a loopback spelling was read as a peer")
    case(f"predicate says {spelling} is NOT remote",
         _ssh_target_is_remote_late(["ssh", spelling, "x"]) is False)

print()
print("=== NON-VACUITY: these cases must be able to fail ===")
#  If the exemption were deleted, the ALLOW block must go red. Prove the
#  subject is load-bearing by driving the predicate directly.
from cron.lifecycle_guard import _ssh_target_is_remote  # noqa: E402

case("predicate says peer host IS remote",
     _ssh_target_is_remote(["ssh", SYNTH, "systemctl"]) is True)
case("predicate says localhost is NOT remote",
     _ssh_target_is_remote(["ssh", "localhost", "systemctl"]) is False)
case("predicate says own hostname is NOT remote",
     _ssh_target_is_remote(["ssh", HOST, "systemctl"]) is False)
case("FIXTURE non-vacuity: neither destination is this host",
     SYNTH.split(".", 1)[0] != HOST and PEER.split(".", 1)[0] != HOST,
     f"fixture picked its own host ({HOST}); the exemption cases are vacuous")
case("FIXTURE non-vacuity: this box is a known real box",
     HOST in REAL_BOXES,
     f"HOST={HOST} matches neither real box; PEER={PEER} is a guess")
case("predicate fails closed on empty segment",
     _ssh_target_is_remote([]) is False)
case("predicate fails closed on non-ssh leader",
     _ssh_target_is_remote(["scp", SYNTH, "x"]) is False)
case("predicate skips option VALUES when finding the destination",
     _ssh_target_is_remote(
         ["ssh", "-i", "/home/will/.ssh/id_ed25519", "-o", "BatchMode=yes",
          SYNTH, "systemctl"]) is True,
     "an option value was mistaken for the host")
case("predicate does not mistake an option value FOR a host",
     _ssh_target_is_remote(["ssh", "-F", "/dev/null", "localhost", "x"]) is False,
     "/dev/null was read as the destination, so localhost was never checked")

print()
if fails:
    print(f"  {len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print(f"  ALL PASS ({ran} cases)")
