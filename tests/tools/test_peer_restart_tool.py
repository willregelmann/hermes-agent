"""restart_peer_gateway resolves the peer and key under the CALLING profile (#54).

Two profiles register a peer under the same name at different gateways with different keys. Run the
real tool under A, then B, then A against real loopback servers: each request must reach its own
profile's gateway with its own key. Also: the tool is offered only when peers are configured.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

from agent.secret_scope import reset_secret_scope, set_secret_scope
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.peer_restart_tool import check_peers_configured, restart_peer_gateway


class _Gateway:
    def __init__(self):
        self.seen = []
        outer = self

        class _H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.seen.append((self.path, self.headers.get("Authorization")))
                body = json.dumps({"restarting": True, "draining": 0}).encode()
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


def _home(tmp_path, name, peers):
    home = tmp_path / name
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"bot_peers": peers}), encoding="utf-8")
    return home


def _as_profile(home, secrets, fn):
    h, s = set_hermes_home_override(home), set_secret_scope(secrets)
    try:
        return fn()
    finally:
        reset_secret_scope(s)
        reset_hermes_home_override(h)


def test_each_profile_restarts_its_own_peer_with_its_own_key(tmp_path):
    gw_a, gw_b = _Gateway(), _Gateway()
    try:
        home_a = _home(tmp_path, "a", {"wren": {"url": gw_a.url}})
        home_b = _home(tmp_path, "b", {"wren": {"url": gw_b.url}})
        run_a = lambda: _as_profile(home_a, {"HERMES_PEER_WREN_KEY": "key-a"},
                                    lambda: restart_peer_gateway("wren"))
        run_b = lambda: _as_profile(home_b, {"HERMES_PEER_WREN_KEY": "key-b"},
                                    lambda: restart_peer_gateway("wren"))
        results = [json.loads(run()) for run in (run_a, run_b, run_a)]
        assert all(r.get("success") and r.get("restarting") for r in results), results
        assert gw_a.seen == [("/api/gateway/restart", "Bearer key-a")] * 2
        assert gw_b.seen == [("/api/gateway/restart", "Bearer key-b")]
    finally:
        gw_a.close()
        gw_b.close()


def test_tool_is_offered_only_when_peers_are_configured(tmp_path):
    with_peers = _home(tmp_path, "with", {"wren": {"url": "http://127.0.0.1:1"}})
    without = _home(tmp_path, "without", {})
    assert _as_profile(with_peers, {}, check_peers_configured) is True
    assert _as_profile(without, {}, check_peers_configured) is False
