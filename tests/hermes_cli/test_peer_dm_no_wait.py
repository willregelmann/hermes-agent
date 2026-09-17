"""--no-wait must not discard a reply the peer already finished.

Issue #3's sender half (PR #12) added ``--no-wait``: send ``wait: false`` and
return as soon as the peer accepts, because the reply arrives later as an
inbound DM.  The receiver half is not built, so EVERY peer today ignores the
unknown key, runs the turn synchronously and returns the finished reply in the
same response.  The sender printed "reply will arrive as an inbound DM" over
the top of it -- discarding a completed answer and promising a DM nobody will
send.  That is the lost-reply bug of issue #3 with the sender doing the losing.

These assert the RESPONSE SHAPE decides, not a version or capability probe:
assistant content present means completed, absent means accepted.
"""
import argparse
import json

import pytest

from hermes_cli.subcommands import peer as peer_mod


REPLY = "REAL REPLY, the whole answer, not a promise of one."


class _FakePeer:
    """Records POST bodies; answers /api/sessions and .../chat."""

    def __init__(self, *, honors_no_wait):
        self.honors_no_wait = honors_no_wait
        self.bodies = []
        self.chat_timeouts = []

    def request(self, url, key, *, method="GET", body=None, timeout=None, headers=None):
        if method == "GET":
            return {"data": [{"id": "sess-1", "title": "Bot Chat"}]}
        self.bodies.append(body)
        self.chat_timeouts.append(timeout)
        if not url.endswith("/chat"):
            raise AssertionError("unexpected POST to %s" % url)
        if self.honors_no_wait and body.get("wait") is False:
            # A real enqueue-and-return: no assistant content exists yet.
            return {"object": "hermes.session.chat.accepted",
                    "session_id": "sess-1", "message_id": "m-9"}
        return {"object": "hermes.session.chat.completion",
                "session_id": "sess-1",
                "message": {"role": "assistant", "content": REPLY},
                "usage": {}, "runtime": {}}


@pytest.fixture
def dm(monkeypatch):
    def run(*, honors_no_wait, no_wait, as_json=False):
        fake = _FakePeer(honors_no_wait=honors_no_wait)
        monkeypatch.setattr(peer_mod, "_load_peers",
                            lambda: {"p": {"url": "http://127.0.0.1:1"}})
        monkeypatch.setattr(peer_mod, "_peer_secret", lambda name: "k")
        monkeypatch.setattr(peer_mod, "_request", fake.request)
        args = argparse.Namespace(peer_action="dm", target="p",
                                  message="a question worth asking",
                                  json=as_json, no_wait=no_wait)
        rc = peer_mod.cmd_peer(args)
        return rc, fake
    return run


def test_control_default_wait_prints_the_reply(dm, capsys):
    """NON-VACUITY. If this ever stops printing the reply the rest mean nothing."""
    rc, fake = dm(honors_no_wait=False, no_wait=False)
    out = capsys.readouterr()
    assert rc == 0
    assert REPLY in out.out
    assert "wait" not in fake.bodies[-1], "default path must not send the flag"


def test_no_wait_against_an_old_peer_prints_the_finished_reply(dm, capsys):
    """THE BUG. The peer ignored wait:false and answered; do not discard it."""
    rc, fake = dm(honors_no_wait=False, no_wait=True)
    out = capsys.readouterr()
    assert rc == 0
    assert fake.bodies[-1].get("wait") is False, "the flag was still sent"
    assert REPLY in out.out, "a completed reply was in hand and was not shown"
    assert "inbound DM" not in out.out, "promised a DM that will never arrive"
    assert "does not support --no-wait" in out.err


def test_no_wait_against_an_old_peer_json_carries_the_reply(dm, capsys):
    rc, _ = dm(honors_no_wait=False, no_wait=True, as_json=True)
    payload = json.loads(capsys.readouterr().out.strip())
    assert rc == 0
    assert payload["reply"] == REPLY
    assert payload["honored_no_wait"] is False
    assert payload["accepted"] is True


def test_no_wait_against_a_real_async_peer_still_returns_immediately(dm, capsys):
    """The feature must survive its own fix: no content means genuinely accepted."""
    rc, _ = dm(honors_no_wait=True, no_wait=True)
    out = capsys.readouterr()
    assert rc == 0
    assert "accepted by 'p'" in out.out
    assert "inbound DM" in out.out
    assert "does not support" not in out.err


def test_no_wait_async_peer_json_says_the_flag_was_honored(dm, capsys):
    rc, _ = dm(honors_no_wait=True, no_wait=True, as_json=True)
    payload = json.loads(capsys.readouterr().out.strip())
    assert rc == 0
    assert payload["honored_no_wait"] is True
    assert "reply" not in payload


def test_empty_assistant_content_is_treated_as_accepted_not_as_a_reply(dm, monkeypatch, capsys):
    """A peer answering with a blank message must not print an empty 'reply'."""
    fake = _FakePeer(honors_no_wait=False)

    def blank(url, key, *, method="GET", body=None, timeout=None, headers=None):
        res = fake.request(url, key, method=method, body=body, timeout=timeout)
        if isinstance(res.get("message"), dict):
            res["message"]["content"] = "   "
        return res

    monkeypatch.setattr(peer_mod, "_load_peers",
                        lambda: {"p": {"url": "http://127.0.0.1:1"}})
    monkeypatch.setattr(peer_mod, "_peer_secret", lambda name: "k")
    monkeypatch.setattr(peer_mod, "_request", blank)
    rc = peer_mod.cmd_peer(argparse.Namespace(peer_action="dm", target="p",
                                              message="m", json=False, no_wait=True))
    out = capsys.readouterr()
    assert rc == 0
    assert "accepted by 'p'" in out.out


def test_no_wait_uses_the_short_accept_timeout_not_the_long_dm_one(dm):
    """The POINT of --no-wait is returning promptly.

    Found 2026-09-17 by mutation audit (wren:i27 round 5): replacing
    ``DM_TIMEOUT_S if wait else ACCEPT_TIMEOUT_S`` with a bare ``DM_TIMEOUT_S``
    SURVIVED every case in this file and in test_peer_cmd.py.  Nothing asserted
    the one number that decides whether the flag returns in 30s or blocks the
    caller for 10 minutes against exactly the old peer this suite exists for.

    Asserted as a RELATION plus the identity, not as a frozen literal: the
    accept path must be the shorter of the two and must be the named constant.
    """
    _, fake = dm(honors_no_wait=True, no_wait=True)
    assert fake.chat_timeouts[-1] == peer_mod.ACCEPT_TIMEOUT_S
    assert peer_mod.ACCEPT_TIMEOUT_S < peer_mod.DM_TIMEOUT_S


def test_default_wait_path_keeps_the_long_timeout(dm):
    """NON-VACUITY for the case above: the two paths must differ."""
    _, fake = dm(honors_no_wait=False, no_wait=False)
    assert fake.chat_timeouts[-1] == peer_mod.DM_TIMEOUT_S
