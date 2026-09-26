"""tell_partner (#52): an agent partner gets the intent in ITS session for this agent pair.

Runs the real tool, directory and ``hermes peer`` transport against a real loopback peer gateway:
Wren, in its conversation with Britta, tells Ash something. It must land in Ash's ``Peer: wren``
session (created on first contact), without waiting for Ash's turn, with Wren as the reply
address and Britta named as who asked, and leave a delivered handoff row behind.
"""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from agent.secret_scope import reset_secret_scope, set_secret_scope
from gateway.handoff import OPEN
from gateway.partner_handoff import handoff_store
from gateway.session_context import clear_session_vars, set_session_vars
from tools.tell_partner_tool import check_partners_configured, tell_partner

PARTNERS = {
    "britta": {"kind": "human", "primary": {"platform": "telegram", "chat_id": "britta-chat"}},
    "ash": {"kind": "agent", "peer": "ash"},
}


class _PeerGateway:
    """Ash's api_server: session listing by title, session create, accept-only chat."""

    def __init__(self):
        self.sessions = {}  # title -> id
        self.chats = []
        outer = self

        class _H(BaseHTTPRequestHandler):
            def _reply(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                title = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("title", [""])[0]
                data = [{"id": sid, "title": t} for t, sid in outer.sessions.items() if t == title]
                self._reply(200, {"data": data})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                if self.path == "/api/sessions":
                    sid = f"s{len(outer.sessions) + 1}"
                    outer.sessions[body["title"]] = sid
                    return self._reply(201, {"session": {"id": sid, "title": body["title"]}})
                outer.chats.append((self.path, body))
                self._reply(202, {"object": "hermes.session.chat.accepted",
                                  "session_id": self.path.split("/")[3], "message_id": "m1"})

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def wren(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "identity.json").write_text(json.dumps({"agent": "wren", "host": "ha-pi"}), encoding="utf-8")
    gateway = _PeerGateway()
    (home / "config.yaml").write_text(yaml.safe_dump(
        {"partners": PARTNERS, "bot_peers": {"ash": {"url": gateway.url}}}), encoding="utf-8")
    scope = set_secret_scope({"HERMES_PEER_ASH_KEY": "k"})
    session = set_session_vars(platform="telegram", chat_id="britta-chat", session_id="britta-sess")
    yield home, gateway
    clear_session_vars(session)
    reset_secret_scope(scope)
    gateway.close()


def test_agent_partner_gets_the_intent_in_its_pair_session(wren):
    home, ash = wren

    for _ in range(2):  # the second handoff reuses the pair session, it doesn't mint another
        result = json.loads(tell_partner("ash", "Britta wants tomorrow's grocery list"))
        # Ash's server accepts (202) without running the turn, so this box cannot know the turn
        # happened: the tool says queued and the row stays open (gateway/partner_handoff.py).
        assert result.get("success") and result["status"] == "queued", result

    assert list(ash.sessions) == ["Peer: wren"]
    assert [path for path, _ in ash.chats] == ["/api/sessions/s1/chat"] * 2
    body = ash.chats[-1][1]
    assert body["wait"] is False and body["reply_to"]["agent"] == "wren"
    assert "Britta wants tomorrow's grocery list" in body["message"]
    assert "britta" in body["message"]  # who asked, resolved from this conversation
    row = handoff_store(str(home)).get(result["handoff_id"])
    assert row.status == OPEN and row.from_session == "britta-sess" and row.extra["partner"] == "ash"


def test_intent_copied_from_a_compressed_call_is_refused_not_sent(wren):
    """An agent imitating compressor-truncated history (msg 23836 on ha-pi) wrote intents cut at
    ~200 chars ending in the compressor's marker. Sending one hands the partner half a message and
    reports success; it must fail loud, before any handoff opens, and say to write it in full."""
    home, ash = wren
    cut = ("Britta asked me to pass on the plan for Saturday: the grocery run moves to the morning "
           "because the toddler's nap shifted, and she wants you to check whether the Costco list "
           "from last week still ...[truncated]")

    result = json.loads(tell_partner("ash", cut))

    assert "error" in result and not result.get("success"), result
    assert "truncated" in result["error"] and "full" in result["error"]
    assert ash.sessions == {} and ash.chats == []  # nothing reached the partner


def test_intent_that_only_mentions_the_marker_is_sent(wren):
    """The control for the refusal above: reporting this bug means quoting the marker mid-text
    (handoff b811d4077ab4 did), and that intent is whole. Only a trailing marker is a cut."""
    home, ash = wren
    result = json.loads(tell_partner("ash", "Review the intents cut at ~200 chars that end in "
                                            "'...[truncated]' and say which fix you'd ship."))
    assert result.get("success") and result["status"] == "queued", result
    assert len(ash.chats) == 1


def test_refusals_say_what_to_do_instead(wren, tmp_path, monkeypatch):
    unknown = json.loads(tell_partner("carol", "hi"))
    assert "ash" in unknown["error"] and "britta" in unknown["error"]
    # A person is reached through the messaging gateway; with none running there is no way in.
    person = json.loads(tell_partner("britta", "Ash says the list is ready"))
    assert "gateway" in person["error"]

    assert check_partners_configured() is True
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(bare))
    assert check_partners_configured() is False
