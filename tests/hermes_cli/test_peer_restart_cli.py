"""`hermes peer restart` shares the tool's transport, and the lifecycle guard lets it through (#54).

The guard blocks self-directed gateway lifecycle commands; asking a PEER's gateway to restart over
its API is not one, so an agent running the CLI verb from its terminal must not be refused.
"""

import argparse

from cron.lifecycle_guard import scan_gateway_lifecycle
from hermes_cli.subcommands import peer as peer_mod


def test_cli_verb_goes_through_the_shared_transport(monkeypatch):
    calls = []

    def fake(target):
        calls.append(target)
        return {"peer": "wren", "profile": "house", "restarting": True, "draining": 1}

    monkeypatch.setattr(peer_mod, "request_peer_gateway_restart", fake)
    rc = peer_mod.cmd_peer(argparse.Namespace(peer_action="restart", target="wren/house", json=True))
    assert (rc, calls) == (0, ["wren/house"])

    def unsupported(target):
        raise peer_mod.PeerRestartUnsupported("old peer")

    monkeypatch.setattr(peer_mod, "request_peer_gateway_restart", unsupported)
    assert peer_mod.cmd_peer(argparse.Namespace(peer_action="restart", target="wren", json=False)) == 1


def test_lifecycle_guard_does_not_block_peer_restart():
    assert scan_gateway_lifecycle("hermes peer restart wren")[0] is False
    # Control: the guard is live for the self-directed form it exists to stop.
    assert scan_gateway_lifecycle("hermes gateway restart")[0] is True
