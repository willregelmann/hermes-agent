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
SSH = ("ssh -F /dev/null -o BatchMode=yes -o ConnectTimeout=8 "
       "-i /home/will/.ssh/id_ed25519 will@will-MS-7B93.local")

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
     blocked("myssh will-MS-7B93.local systemctl --user restart hermes-gateway") is True)
case("no-ssh remote-looking prose is still blocked",
     blocked("will-MS-7B93.local: systemctl --user restart hermes-gateway") is True)

print()
print("=== MUST NOW ALLOW (the peer-restart path) ===")
case("plain ssh to a PEER host is allowed",
     blocked("ssh will-MS-7B93.local systemctl --user restart hermes-gateway") is False,
     "still blocked")
case("ssh with user@peer is allowed",
     blocked("ssh will@will-MS-7B93.local systemctl --user restart hermes-gateway") is False,
     "still blocked")
case("ssh with the real option soup I actually use is allowed",
     blocked(f"{SSH} 'systemctl --user restart hermes-gateway'") is False,
     "still blocked")
case("absolute-path ssh binary is allowed",
     blocked("/usr/bin/ssh will-MS-7B93.local systemctl --user restart hermes-gateway") is False,
     "still blocked")
case("peer hermes gateway restart is allowed",
     blocked("ssh will-MS-7B93.local hermes gateway restart") is False,
     "still blocked")

print()
print("=== NON-VACUITY: these cases must be able to fail ===")
#  If the exemption were deleted, the ALLOW block must go red. Prove the
#  subject is load-bearing by driving the predicate directly.
from cron.lifecycle_guard import _ssh_target_is_remote  # noqa: E402

case("predicate says peer host IS remote",
     _ssh_target_is_remote(["ssh", "will-MS-7B93.local", "systemctl"]) is True)
case("predicate says localhost is NOT remote",
     _ssh_target_is_remote(["ssh", "localhost", "systemctl"]) is False)
case("predicate says own hostname is NOT remote",
     _ssh_target_is_remote(["ssh", HOST, "systemctl"]) is False)
case("predicate fails closed on empty segment",
     _ssh_target_is_remote([]) is False)
case("predicate fails closed on non-ssh leader",
     _ssh_target_is_remote(["scp", "will-MS-7B93.local", "x"]) is False)
case("predicate skips option VALUES when finding the destination",
     _ssh_target_is_remote(
         ["ssh", "-i", "/home/will/.ssh/id_ed25519", "-o", "BatchMode=yes",
          "will-MS-7B93.local", "systemctl"]) is True,
     "an option value was mistaken for the host")
case("predicate does not mistake an option value FOR a host",
     _ssh_target_is_remote(["ssh", "-F", "/dev/null", "localhost", "x"]) is False,
     "/dev/null was read as the destination, so localhost was never checked")

print()
if fails:
    print(f"  {len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print(f"  ALL PASS ({ran} cases)")
