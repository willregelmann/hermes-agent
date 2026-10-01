"""E2E: an agent-to-agent turn reaches the receiving api_server labelled as a bot, from the SENDER.

Before this, ``hermes peer dm``, ``hermes peer run`` and ``send_to_peer`` (the transport behind
``tell_partner`` handoffs and alarm-woken peer replies) sent ``author`` only when a dispatcher
had set HERMES_TURN_AUTHOR. An agent running any of them sent no author, so the receiver's
memory providers recorded the peer's words as the user's. Measured on three live Tapestry minds:
the peer-session rows were filed under source=user.

The receiving side is a real APIServerAdapter on a real loopback socket with a real state.db;
only the model turn (``_run_agent``) is stubbed so we can read the ``turn_author`` it was handed.
The sending side is the stock client code under its own HERMES_HOME, which holds the identity.
"""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from aiohttp import web

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_cli.subcommands import peer as peer_cmd
from hermes_state import SessionDB

API_KEY = "sk-author-e2e-0123456789"
SENDER = "alice"
RECEIVER = "bob"


def _identity(home, name):
    home.mkdir(parents=True, exist_ok=True)
    (home / "identity.json").write_text(json.dumps({"schema": 1, "agent": name}), encoding="utf-8")


@pytest.fixture()
def receiver(tmp_path):
    """The receiving gateway: its own home (identity ``bob``) and state.db on a real socket."""
    home = tmp_path / "receiver_home"
    _identity(home, RECEIVER)
    db = SessionDB(home / "state.db")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": API_KEY}))
    adapter._session_db = db
    seen = []

    async def fake_run_agent(user_message, **kwargs):
        seen.append({"message": user_message, "turn_author": kwargs.get("turn_author")})
        return {"final_response": "ok", "session_id": kwargs.get("session_id")}, {}

    adapter._run_agent = fake_run_agent
    app = web.Application()
    app.router.add_get("/api/sessions", adapter._handle_list_sessions)
    app.router.add_post("/api/sessions", adapter._handle_create_session)
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)

    loop = asyncio.new_event_loop()
    started, state = threading.Event(), {}

    def _serve():
        asyncio.set_event_loop(loop)

        async def _start():
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            state["runner"], state["port"] = runner, runner.addresses[0][1]
            started.set()

        loop.run_until_complete(_start())
        loop.run_forever()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    assert started.wait(timeout=10), "receiver gateway failed to start"
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{state['port']}", db=db, seen=seen)
    finally:
        asyncio.run_coroutine_threadsafe(state["runner"].cleanup(), loop).result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)


@pytest.fixture()
def sender(tmp_path, monkeypatch, receiver):
    """The sending agent: its own HERMES_HOME holding identity ``alice``, no dispatcher author."""
    home = tmp_path / "sender_home"
    _identity(home, SENDER)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_TURN_AUTHOR", raising=False)
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"bob": {"url": receiver.url}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: API_KEY)
    return home


def _assert_sender_bot(author):
    # Who reached the receiver: the sender, as a bot. Never the receiver's own identity.
    assert author is not None, "agent turn arrived with no author; providers will file it as the user's"
    assert author["is_bot"] is True
    assert author["name"] == SENDER
    assert author["id"].startswith("bot:") and author["id"].endswith(f"/{SENDER}")
    assert RECEIVER not in (author["name"], author["id"].rsplit("/", 1)[-1])


def test_peer_dm_carries_sender_bot_author(receiver, sender):
    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="bob", message="disk status?", json=True))

    assert rc == 0
    assert [s["message"] for s in receiver.seen] == ["disk status?"], "the turn never reached the receiver"
    _assert_sender_bot(receiver.seen[0]["turn_author"])


def test_send_to_peer_handoff_path_carries_sender_bot_author(receiver, sender):
    # send_to_peer is what tell_partner's deliver_to_agent and alarm-woken peer replies call.
    # wait=False is honoured by the real gateway with a background run; poll for the stubbed turn.
    result = peer_cmd.send_to_peer("bob", "[Handoff from alice's conversation with will] hello")

    assert result["accepted"] is True
    for _ in range(100):
        if receiver.seen:
            break
        threading.Event().wait(0.05)
    assert receiver.seen, "the handoff never reached the receiver's turn"
    _assert_sender_bot(receiver.seen[0]["turn_author"])


def test_multiplexed_sender_profile_names_itself_not_the_launch_profile(receiver, sender, tmp_path):
    """A multiplexed gateway launched as one agent serves another profile's tell_partner: the handoff
    must carry the SERVED profile's identity (contextvar scope), not the launch home in os.environ."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    served = tmp_path / "served_profile_home"
    _identity(served, "carol")
    token = set_hermes_home_override(str(served))
    try:
        peer_cmd.send_to_peer("bob", "from the served profile")
    finally:
        reset_hermes_home_override(token)
    for _ in range(100):
        if receiver.seen:
            break
        threading.Event().wait(0.05)
    author = receiver.seen[0]["turn_author"]
    assert author["name"] == "carol" and author["id"].endswith("/carol") and author["is_bot"] is True


def test_dispatcher_author_still_wins(receiver, sender, monkeypatch):
    """A Bot Mode dispatcher that set HERMES_TURN_AUTHOR keeps its more specific author."""
    relayed = {"id": "bot:conn7/carol", "name": "carol", "is_bot": True}
    monkeypatch.setenv("HERMES_TURN_AUTHOR", json.dumps(relayed))

    peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="bob", message="relayed", json=True))

    assert receiver.seen[0]["turn_author"] == relayed


def test_human_over_api_server_is_not_labelled_a_bot(receiver):
    """Contrast arm: a person's own client (no author in the body) still arrives authorless,
    which providers record as the user. Without this, labelling every turn a bot would pass."""
    import urllib.request

    sid = receiver.db.create_session("will-chat", "api_server")
    req = urllib.request.Request(
        f"{receiver.url}/api/sessions/{sid}/chat", method="POST",
        data=json.dumps({"message": "hi, it's Will"}).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200

    assert receiver.seen == [{"message": "hi, it's Will", "turn_author": None}]


def test_no_identity_sends_no_author(receiver, sender):
    """No declared identity: say nothing rather than invent a sender."""
    (sender / "identity.json").unlink()

    peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="bob", message="anon", json=True))

    assert receiver.seen[0]["turn_author"] is None
