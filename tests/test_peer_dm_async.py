"""Sender-side async peer DM: a slow peer must not read as an unreachable one.

WHY A REAL SERVER AND NOT MOCKS
The defect being fixed IS socket behaviour: a POST that blocks for the peer's
entire agent turn, and an `except` arm that collapses TimeoutError into the
same sentence as URLError. Mocking `_request` would assert that the code calls
the function we told it to call — it could not observe the one thing that
matters, which is what happens when a real server accepts a connection and
then goes quiet. So every case here drives a real http.server on loopback.

Ash's review rule applies to my own work: at least one case must construct its
subject the way production does. Here that means the actual argparse parser
and the actual `cmd_peer` entry point, not a hand-built namespace — a flag
that is never registered is a flag that silently never activates, which is
how the recall-indicator gate shipped inert.

Run: python3 tests/test_peer_dm_async.py   (no pytest needed)
     or under pytest via tests/test_peer_dm_async_pytest_entry.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_cli.subcommands import peer as P  # noqa: E402

fails: list[str] = []
ran = 0


def case(name: str, ok: bool, detail: str = "") -> None:
    global ran
    ran += 1
    if ok:
        print(f"  PASS  {name}")
    else:
        fails.append(name)
        print(f"  FAIL  {name}  {detail}")


# --------------------------------------------------------------------------
# A real peer. `delay` simulates an agent turn that takes real time.
# --------------------------------------------------------------------------
class FakePeer:
    def __init__(self, *, delay: float = 0.0, honour_wait: bool = True):
        self.delay = delay
        self.honour_wait = honour_wait
        self.requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _json(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                # Session lookup used by _ensure_bot_chat / _find_bot_chat.
                self._json(200, {"sessions": [
                    {"id": "sess-bot-chat", "title": P.BOT_CHAT_TITLE},
                ]})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append({"path": self.path, "body": body})

                # ROUTE ON PATH. Treating every POST as a chat turn made the
                # session-create call sleep for the turn delay too, so an
                # accept-only send still took 3s and the case failed against
                # its own fixture rather than the code. A fixture that does
                # not distinguish the endpoints cannot measure the endpoint.
                if not self.path.endswith("/chat"):
                    self._json(200, {"session": {"id": "sess-bot-chat",
                                                 "title": P.BOT_CHAT_TITLE}})
                    return

                wants_async = body.get("wait") is False
                if wants_async and outer.honour_wait:
                    # Enqueue and answer immediately — the whole point.
                    self._json(200, {"session_id": "sess-bot-chat",
                                     "message_id": "m-1", "accepted": True})
                    return
                # Otherwise behave like today: block for the turn.
                time.sleep(outer.delay)
                try:
                    self._json(200, {"session_id": "sess-bot-chat",
                                     "message": {"content": "done thinking"}})
                except BrokenPipeError:
                    # Expected in case e: the client's socket timed out while
                    # this handler was still "thinking". That broken pipe IS
                    # the bug's mechanism, not a fixture defect.
                    pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


class Args:
    """Minimal args object for the paths that don't go through argparse."""
    def __init__(self, **kw):
        # cmd_peer dispatches on `peer_action`, NOT `action`. Getting this
        # wrong silently routed every case into the `list` branch and made
        # four assertions fail against output they never produced — caught
        # only by running it.
        self.peer_action = "dm"
        self.target = "testpeer"
        self.message = "hello"
        self.json = False
        self.no_wait = False
        for k, v in kw.items():
            setattr(self, k, v)


def run_dm(port: int, **kw) -> tuple[int, str, str]:
    """Drive the real cmd_peer against a real server, capturing output.

    The peer must exist in the REGISTRY, not just have a stubbed base url:
    cmd_peer resolves the peer record before it ever builds a URL, so
    stubbing only `_base_url` left every case failing on "No peer named".
    Register a real entry in a jailed HOME so the production resolution path
    runs — and so the suite cannot touch the real ~/.hermes/peers.json.
    """
    os.environ["HERMES_PEER_TESTPEER_KEY"] = "k"
    out, err = io.StringIO(), io.StringIO()
    args = Args(**kw)
    orig = P._base_url
    P._base_url = lambda peer, profile: f"http://127.0.0.1:{port}"
    orig_load = P._load_peers
    P._load_peers = lambda: {"testpeer": {"url": f"http://127.0.0.1:{port}"}}
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = P.cmd_peer(args)
    finally:
        P._base_url = orig
        P._load_peers = orig_load
    return rc, out.getvalue(), err.getvalue()


print("=== sender-side async peer DM ===")

# --- a: the constant actually changed --------------------------------------
case("a ACCEPT_TIMEOUT_S exists and is a real transport timeout",
     getattr(P, "ACCEPT_TIMEOUT_S", None) == 30, f"got {getattr(P,'ACCEPT_TIMEOUT_S',None)}")
case("a DM_TIMEOUT_S still 600 for the blocking path (unchanged default)",
     P.DM_TIMEOUT_S == 600, f"got {P.DM_TIMEOUT_S}")

# --- b: PRODUCTION SHAPE — the flag is registered on the real parser -------
#     A flag that argparse never learned about is a flag that silently never
#     activates. This builds the parser through build_peer_parser(), the same
#     function the CLI calls, rather than asserting against a hand-made
#     namespace — which would have passed while the flag did not exist.
import argparse  # noqa: E402

_root = argparse.ArgumentParser()
_sub = _root.add_subparsers(dest="command")
P.build_peer_parser(_sub)
try:
    ns = _root.parse_args(["peer", "dm", "ash", "hi", "--no-wait"])
    case("b --no-wait is registered on the REAL parser",
         getattr(ns, "no_wait", None) is True and ns.peer_action == "dm",
         f"no_wait={getattr(ns,'no_wait',None)} peer_action={getattr(ns,'peer_action',None)}")
    ns2 = _root.parse_args(["peer", "dm", "ash", "hi"])
    case("b default is wait (absent flag means the old behaviour)",
         getattr(ns2, "no_wait", None) is False, f"no_wait={getattr(ns2,'no_wait',None)}")
except SystemExit as exc:
    case("b --no-wait is registered on the REAL parser", False,
         f"argparse rejected the flag (exit {exc.code}) — not registered")
    case("b default is wait (absent flag means the old behaviour)", False, "parser build failed")

# --- c: accept-only returns immediately even when the turn is slow ---------
with FakePeer(delay=3.0) as peer:
    t0 = time.time()
    rc, out, err = run_dm(peer.port, no_wait=True)
    elapsed = time.time() - t0
    case("c --no-wait returns before the turn finishes",
         rc == 0 and elapsed < 2.0, f"rc={rc} elapsed={elapsed:.2f}s err={err[:120]}")
    case("c it reports acceptance, not a fabricated reply",
         "accepted" in out.lower(), f"out={out[:120]}")
    case("c the session id is printed so a late reply is recoverable",
         "sess-bot-chat" in out, f"out={out[:120]}")
    sent = [r for r in peer.requests if "/chat" in r["path"]]
    case("c the wait=False flag actually crossed the wire",
         bool(sent) and sent[-1]["body"].get("wait") is False,
         f"sent={sent[-1]['body'] if sent else None}")

# --- d: default path is unchanged (no silent behaviour change) ------------
with FakePeer(delay=0.0) as peer:
    rc, out, err = run_dm(peer.port)
    case("d default still waits and prints the reply",
         rc == 0 and "done thinking" in out, f"rc={rc} out={out[:120]}")
    sent = [r for r in peer.requests if "/chat" in r["path"]]
    case("d default sends NO wait flag (old peers unaffected)",
         bool(sent) and "wait" not in sent[-1]["body"],
         f"body={sent[-1]['body'] if sent else None}")

# --- e: THE BUG — a slow peer must not read as unreachable ----------------
#     Force the timeout by shrinking it, with a server that is alive and
#     working. Pre-fix this produced "Could not reach peer" and exit 1.
_orig_accept = P.ACCEPT_TIMEOUT_S
P.ACCEPT_TIMEOUT_S = 1
try:
    with FakePeer(delay=4.0, honour_wait=False) as peer:
        rc, out, err = run_dm(peer.port, no_wait=True)
        case("e a working-but-slow peer is NOT reported unreachable",
             "could not reach" not in err.lower(), f"err={err[:160]}")
        case("e the message says the turn is still running",
             "still running" in err.lower(), f"err={err[:160]}")
        case("e timeout gets its own exit code, distinct from unreachable",
             rc == 3, f"rc={rc}")
        case("e the session id is named so the reply can be recovered",
             "sess-bot-chat" in err, f"err={err[:160]}")
finally:
    P.ACCEPT_TIMEOUT_S = _orig_accept

# --- f: MUTANT — the pre-fix collapsed except arm -------------------------
#     Reproduces the old behaviour: TimeoutError caught in the same arm as
#     URLError/OSError. Observable difference: mutant says "could not reach".
import urllib.error  # noqa: E402


def mutant_report(exc: Exception) -> tuple[str, int]:
    """The arm as it was before this change."""
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return ("Could not reach peer 'ash': %s" % exc, 1)
    raise AssertionError("unreachable")


def fixed_report(exc: Exception) -> tuple[str, int]:
    if isinstance(exc, TimeoutError):
        return ("Peer 'ash' did not answer within Ns — the turn is likely still "
                "running. This is NOT an unreachable peer: %s" % exc, 3)
    if isinstance(exc, (urllib.error.URLError, OSError)):
        return ("Could not reach peer 'ash': %s" % exc, 1)
    raise AssertionError("unreachable")


t_exc = TimeoutError("timed out")
m_msg, m_rc = mutant_report(t_exc)
f_msg, f_rc = fixed_report(t_exc)
case("f MUTANT reports a completed-but-slow turn as unreachable",
     "could not reach" in m_msg.lower() and m_rc == 1, f"{m_msg} rc={m_rc}")
case("f FIXED distinguishes it (mutant killed by observable difference)",
     "not an unreachable peer" in f_msg.lower() and f_rc == 3, f"{f_msg} rc={f_rc}")
case("f non-vacuity: both arms saw the same exception object",
     m_msg != f_msg and "timed out" in m_msg and "timed out" in f_msg)

# a genuinely dead host must STILL report unreachable — the fix must not
# make every failure look like patience.
d_exc = urllib.error.URLError("connection refused")
d_msg, d_rc = fixed_report(d_exc)
case("f a truly unreachable peer is still reported unreachable",
     "could not reach" in d_msg.lower() and d_rc == 1, f"{d_msg} rc={d_rc}")


# --- g: THE SESSION ID ON EVERY FAILURE ARM --------------------------------
#     The PR body promises: "Every failure after the session id is known now
#     prints it." That sentence is the recovery contract -- a late reply is
#     only recoverable if the handle is on screen -- and no case asserted it
#     for three of the four arms. Mutants that deleted the session_id print
#     from the HTTPError, RuntimeError and URLError/OSError arms all survived
#     the original 18 cases. A claim stated in prose with no case behind it is
#     a documented invariant with no binding.
#
#     Each arm is driven by REAL SERVER BEHAVIOUR, not by a raised sentinel:
#       http500  -> HTTPError        (peer rejects the chat POST)
#       nonjson  -> RuntimeError     (_request cannot parse the body)
#       hangup   -> OSError          (peer closes the socket mid-response)
#     In all three the Bot Chat session resolves FIRST, so session_id is known
#     and the contract applies.


class BreakingPeer:
    """A peer that answers session lookup normally and then fails the chat POST."""

    def __init__(self, mode: str):
        self.mode = mode
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                self._json(200, {"sessions": [
                    {"id": "sess-bot-chat", "title": P.BOT_CHAT_TITLE},
                ]})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                if not self.path.endswith("/chat"):
                    self._json(200, {"session": {"id": "sess-bot-chat",
                                                 "title": P.BOT_CHAT_TITLE}})
                    return
                if outer.mode == "http500":
                    self._json(500, {"error": "boom"})
                elif outer.mode == "nonjson":
                    b = b"<html>not json</html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                else:  # hangup: close without any response at all
                    self.close_connection = True
                    try:
                        self.connection.close()
                    except OSError:
                        pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


for _mode, _label, _expect_rc in (
    ("http500", "g HTTPError", 1),
    ("nonjson", "g RuntimeError", 1),
    ("hangup", "g unreachable/OSError", 1),
):
    with BreakingPeer(_mode) as _bp:
        _rc, _out, _err = run_dm(_bp.port)
        case(f"{_label} arm exits {_expect_rc}",
             _rc == _expect_rc, f"rc={_rc} err={_err[:160]}")
        # NON-VACUITY: the arm must have been reached at all, i.e. the failure
        # is the chat POST and not the session lookup -- otherwise "no session
        # id printed" would be CORRECT rather than a defect.
        case(f"{_label} arm did not fail at session resolution",
             "resolving Bot Chat session" not in _err, f"err={_err[:160]}")
        case(f"{_label} arm prints the session id (recovery contract)",
             "sess-bot-chat" in _err, f"err={_err[:200]}")


# --- h: THE DEFAULT PATH KEEPS ITS 600s PATIENCE ---------------------------
#     `timeout = DM_TIMEOUT_S if wait else ACCEPT_TIMEOUT_S` is a CONDITIONAL,
#     and only its false branch was covered: case c proves --no-wait uses the
#     short timeout, nothing proved the default uses the long one. Collapsing
#     it to `timeout = ACCEPT_TIMEOUT_S` -- which reintroduces the exact bug
#     the PR fixes, a blocking send that gives up after 30s -- passed all 18.
#
#     Asserted as BEHAVIOUR under a shrunk pair, not by reading the constant:
#     ACCEPT=1 would fail a 3s turn, DM=30 must not. The 3s/1s margin is wide
#     and the failing direction is a TIMEOUT, so machine load can only make the
#     fixed subject look broken, never the broken one look fixed (lesson 62).
_sa, _sd = P.ACCEPT_TIMEOUT_S, P.DM_TIMEOUT_S
P.ACCEPT_TIMEOUT_S = 1
P.DM_TIMEOUT_S = 30
try:
    with FakePeer(delay=3.0) as peer:
        rc, out, err = run_dm(peer.port)
        case("h the DEFAULT path uses DM_TIMEOUT_S, not ACCEPT_TIMEOUT_S",
             rc == 0 and "done thinking" in out,
             f"rc={rc} out={out[:80]} err={err[:200]}")
    # Non-vacuity control: the same turn under the SHORT timeout must fail,
    # or the case above would pass no matter which branch was taken.
    with FakePeer(delay=3.0, honour_wait=False) as peer:
        rc2, out2, err2 = run_dm(peer.port, no_wait=True)
        case("h non-vacuity: the same turn DOES time out on the short branch",
             rc2 == 3, f"rc={rc2} err={err2[:160]}")
finally:
    P.ACCEPT_TIMEOUT_S, P.DM_TIMEOUT_S = _sa, _sd

print()
if fails:
    print(f"  {len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print(f"  ALL PASS ({ran} cases)")
