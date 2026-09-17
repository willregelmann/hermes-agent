"""Cross-box delivery: a reply goes back to the SENDER, not to itself.

THE DEFECT THIS ENCODES, measured on a live pair 2026-09-16:
`_deliver_peer_completion` read `session_id` off the completion event. The
producer sets that to the session the TURN RAN IN — a session on the PEER's
box. So the watcher faithfully delivered the reply into the peer's own
transcript. Both halves were internally correct; the reply never crossed the
network. Ash's handoff row 5432a0f3 went open 09:58:01 -> delivered 09:58:26
and my box never saw a thing.

The sender is the only party that knows where the answer should go, so the
sender says. `reply_to` is a hint an old peer ignores, exactly like `wait`.

Run: python3 tests/gateway/test_peer_reply_routing.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

TREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, TREE)

fails: list = []
ran = 0


def check(name, ok, detail=""):
    global ran
    ran += 1
    if ok:
        print(f"  PASS  {name}")
    else:
        fails.append(name)
        print(f"  FAIL  {name}  {detail}")


print("=" * 70)
print("cross-box: a reply returns to the sender")
print(f"  tree: {TREE}")
print("=" * 70)

peer_py = os.path.join(TREE, "hermes_cli", "subcommands", "peer.py")
api_py = os.path.join(TREE, "gateway", "platforms", "api_server.py")
run_py = os.path.join(TREE, "gateway", "run.py")
for p in (peer_py, api_py, run_py):
    if not os.path.isfile(p):
        print(f"SUBJECT ABSENT: {p}")
        sys.exit(2)
check("S1 all three subjects exist", True)

peer_src = open(peer_py, encoding="utf-8").read()
api_src = open(api_py, encoding="utf-8").read()
run_src = open(run_py, encoding="utf-8").read()

# ---- A: the three halves must agree on ONE key name ------------------
check("A1 sender sets reply_to on the --no-wait body",
      'body["reply_to"] = origin' in peer_src,
      "sender declares no return address; --no-wait delivers nowhere")
check("A2 receiver carries reply_to into the completion event",
      '"reply_to": reply_to,' in api_src,
      "accept path drops the return address before publishing")
check("A3 watcher routes on reply_to BEFORE local delivery",
      run_src.find('reply_to = evt.get("reply_to")') != -1
      and run_src.find('reply_to = evt.get("reply_to")')
      < run_src.find("_inject_peer_completion_turn(session_id"),
      "routing decision comes after local injection — reply goes to self")

# ---- B: the sender's return address ----------------------------------
# ORDER IS LOAD-BEARING AND I GOT IT WRONG FIRST. The jailed-home case below
# reloads `hermes_constants` and `peer`; if it runs BEFORE the real-identity
# case, the reloaded modules keep the jail's resolution and B1/B2 fail on a
# perfectly good box. Ask the real question first, then jail.
from hermes_cli.subcommands.peer import _self_origin  # noqa: E402

origin = _self_origin()
check("B1 this box can state its own return address",
      isinstance(origin, dict) and bool(origin.get("agent")),
      f"got {origin!r}")
check("B2 the address is an AGENT NAME, never a session id",
      isinstance(origin, dict) and "session" not in json.dumps(origin).lower(),
      f"{origin!r} — one box must not name a session on another")

# A jailed home with no identity.json must yield None, not a guess. Run this
# in a CHILD interpreter so the reload cannot leak into the cases above or
# below — the leak is exactly what bit me.
import subprocess as _sp  # noqa: E402

jail = tempfile.mkdtemp(prefix="wren-origin-")
_probe = (
    "import os,sys;os.environ['HERMES_HOME']=%r;sys.path.insert(0,%r);"
    "from hermes_cli.subcommands.peer import _self_origin;"
    "print('RESULT=' + repr(_self_origin()))" % (jail, TREE)
)
_r = _sp.run([sys.executable, "-c", _probe], capture_output=True, text=True, timeout=120)
blank_line = [l for l in _r.stdout.splitlines() if l.startswith("RESULT=")]
check("B3 the jailed probe actually ran (non-vacuity)",
      bool(blank_line), f"stdout={_r.stdout[-200:]!r} stderr={_r.stderr[-200:]!r}")
blank = blank_line[0].split("=", 1)[1] if blank_line else "<no result>"
check("B4 no identity -> None, NOT a fabricated address",
      blank == "None",
      f"got {blank}: a wrong return address delivers a real reply nowhere "
      f"while the send still looks successful")

# ---- C: routing, driven on the real method ---------------------------
# Tests run against the REAL production identity.json unless told otherwise,
# and #34's fix means a box with no known peers refuses every address. That is
# correct behaviour, but it would make the in-process arms (C/D/E/G1) depend on
# THIS machine's peer list, which is not their subject. Give them a jailed home
# with a known peer so they measure ROUTING; the H arms below own the
# no-policy question and each use their own home.
_home_jail = tempfile.mkdtemp(prefix="wren-testhome-")
with open(os.path.join(_home_jail, "identity.json"), "w", encoding="utf-8") as _fh:
    json.dump({"agent": "wren", "host": "ha-pi.local",
               "peers": {"ash": {"host": "will-ms-7b93.local"},
                         "wren": {"host": "ha-pi.local"}}}, _fh)
os.environ["HERMES_HOME"] = _home_jail

import gateway.run as R  # noqa: E402
from gateway.handoff import DELIVERED, OPEN, HandoffStore  # noqa: E402

# ONE HOME, ONE STORE. The production closer resolves the handoff store from
# HERMES_HOME, so if the test reads a store at a DIFFERENT path than the
# subject writes, C3 goes red against correct code — the test half and the
# production half never touch the same file. That is the untethered-by-data-
# location defect, and it cost me a red C3 here.
# The same home also needs a peers map, because #34 refuses any address it
# cannot verify. The H arms below use their own throwaway homes.
tmp = tempfile.mkdtemp(prefix="wren-route-")
with open(os.path.join(tmp, "identity.json"), "w", encoding="utf-8") as _fh:
    json.dump({"agent": "wren", "host": "ha-pi.local",
               "peers": {"ash": {"host": "will-ms-7b93.local"},
                         "wren": {"host": "ha-pi.local"}}}, _fh)
os.environ["HERMES_HOME"] = tmp
path = os.path.join(tmp, "handoffs.jsonl")


class FakeRunner:
    _running = True

    def __init__(self, return_ok=True):
        self.returned = []
        self.injected = []
        self._return_ok = return_ok

    async def _return_peer_completion(self, reply_to, text, evt):
        # NOT a stub of the guard: bind the REAL method's validation by
        # calling it, and only fake the subprocess at the very end. Stubbing
        # the whole method would mean the third-party check never executes
        # and F1 would test the fake instead of the subject.
        self.returned.append((reply_to.get("agent"), text))
        return self._return_ok

    async def _inject_peer_completion_turn(self, session_id, text, evt):
        self.injected.append((session_id, text))
        return True


FakeRunner._deliver_peer_completion = R.GatewayRunner._deliver_peer_completion
FakeRunner._close_peer_handoff = R.GatewayRunner._close_peer_handoff
# The REAL validator, not a stub. This is the point of F: the guard must sit
# on the router's path so that faking the action method cannot bypass it.
FakeRunner._reply_to_is_trustworthy = R.GatewayRunner._reply_to_is_trustworthy


async def drive():
    store = HandoffStore(path, author="test")

    # C: a remote sender -> returns, does NOT inject locally
    h = store.open_handoff(from_session="wren", to_session="s1",
                           requesting_user="wren", intent="x")
    evt = {"type": "peer_completion", "session_id": "ash-local-session",
           "text": "pong", "peer": "wren", "handoff_id": h.id,
           "reply_to": {"agent": "wren", "host": "ha-pi.local"}}
    r = FakeRunner()
    ok = await r._deliver_peer_completion(evt)
    check("C1 a reply_to event RETURNS to the sender", ok is True and
          r.returned == [("wren", "pong")], f"returned={r.returned}")
    check("C2 and does NOT inject into the local session",
          r.injected == [],
          f"injected {r.injected} — this IS the live bug: the reply went "
          f"back into the box that produced it")
    rows = [x for x in HandoffStore(path, author="r").all_latest() if x.id == h.id]
    check("C3 a successful return closes the row",
          rows and rows[0].status == DELIVERED, f"{rows[0].status if rows else None}")

    # D: CONTROL — no reply_to (local/old sender) still injects locally.
    # Without this, C2 passes for a subject that never injects at all.
    h2 = store.open_handoff(from_session="local", to_session="s2",
                            requesting_user="local", intent="y")
    evt2 = {"type": "peer_completion", "session_id": "my-session",
            "text": "local answer", "peer": "self", "handoff_id": h2.id}
    r2 = FakeRunner()
    ok2 = await r2._deliver_peer_completion(evt2)
    check("D1 CONTROL: no reply_to still delivers LOCALLY",
          ok2 is True and r2.injected == [("my-session", "local answer")],
          f"injected={r2.injected}")
    check("D2 CONTROL: and does not try to return",
          r2.returned == [], f"returned={r2.returned}")

    # E: a FAILED return must leave the row open
    h3 = store.open_handoff(from_session="wren", to_session="s3",
                            requesting_user="wren", intent="z")
    evt3 = dict(evt, handoff_id=h3.id)
    r3 = FakeRunner(return_ok=False)
    ok3 = await r3._deliver_peer_completion(evt3)
    check("E1 a failed return reports False so the caller requeues",
          ok3 is False, f"got {ok3}")
    rows3 = [x for x in HandoffStore(path, author="r").all_latest() if x.id == h3.id]
    check("E2 a failed return LEAVES THE ROW OPEN (still owed)",
          rows3 and rows3[0].status == OPEN,
          f"{rows3[0].status if rows3 else None}")
    check("E3 NON-VACUITY: success and failure arms differ",
          ok is True and ok3 is False)


    # F: Ash's B1 — a return address naming a THIRD PARTY must be refused.
    #    The address is remote-supplied and goes straight to argv. An event
    #    that arrived FROM wren carrying reply_to.agent=someone-else would
    #    otherwise deliver there, return True, and close the row: loud
    #    success, wrong destination. Checkable only because it is a NAME.
    h4 = store.open_handoff(from_session="wren", to_session="s4",
                            requesting_user="wren", intent="spoof")
    evt4 = {"type": "peer_completion", "session_id": "local", "text": "secret",
            "peer": "wren", "handoff_id": h4.id,
            "reply_to": {"agent": "some-other-peer"}}
    r4 = FakeRunner()
    ok4 = await r4._deliver_peer_completion(evt4)
    check("F1 a reply_to naming a DIFFERENT agent is refused",
          r4.returned == [],
          f"returned={r4.returned} — delivered to an unverified third party")
    check("F2 and it is not injected locally either",
          r4.injected == [], f"injected={r4.injected}")
    check("F3 it DROPS rather than requeues (wrong forever, would spin)",
          ok4 is True, f"got {ok4}")
    rows4 = [x for x in HandoffStore(path, author="r").all_latest() if x.id == h4.id]
    check("F4 the row STAYS OPEN — nothing was delivered",
          rows4 and rows4[0].status == OPEN,
          f"{rows4[0].status if rows4 else None}")
    check("F5 CONTROL: the SAME event with a matching agent DOES return",
          (await FakeRunner()._deliver_peer_completion(
              dict(evt4, handoff_id=store.open_handoff(
                  from_session='w', to_session='s5', requesting_user='w',
                  intent='ok').id,
                   reply_to={"agent": "wren"}))) is True,
          "the refusal arm cannot be distinguished from a subject that never "
          "returns at all")

    # ---- G: THE LIVE SHAPE. Every case above supplied a sender name that no
    # real sender sends. The accept path defaults requesting_user to the
    # literal "peer", so on the live pair the guard refused the ONLY kind of
    # reply that actually occurs:
    #     11:31:46 reply_to.agent='wren' but the turn came from peer='peer'
    # A fixture that supplies a value production never supplies tests a
    # different system. These cases use the real defaulted string.
    h6 = store.open_handoff(from_session="peer", to_session="s6",
                            requesting_user="peer", intent="live shape")
    evt6 = {"type": "peer_completion", "session_id": "local", "text": "ack",
            "peer": "peer", "handoff_id": h6.id,
            "reply_to": {"agent": "ash", "host": "will-ms-7b93.local"}}
    r6 = FakeRunner()
    ok6 = await r6._deliver_peer_completion(evt6)
    check("G1 THE LIVE SHAPE: peer='peer' + a KNOWN agent DELIVERS",
          r6.returned == [("ash", "ack")],
          f"returned={r6.returned} — this is the exact event the live guard "
          f"refused at 11:31:46; if this fails the chain is still broken")
    check("G2 and the row closes on that delivery", ok6 is True, f"got {ok6}")

    # G3/G4 need a REAL identity.json — without one the abstention path fires
    # and the unknown-agent check never runs, which is precisely the failure
    # my own control caught. Point HERMES_HOME at a temp home containing a
    # known-peers file, in a child so nothing leaks.
    import subprocess as _sp2

    jail2 = tempfile.mkdtemp(prefix="wren-known-")
    with open(os.path.join(jail2, "identity.json"), "w", encoding="utf-8") as fh:
        json.dump({"agent": "wren", "host": "ha-pi.local",
                   "peers": {"ash": {"host": "will-ms-7b93.local"}}}, fh)
    probe_src = f'''import os, sys
os.environ["HERMES_HOME"] = {jail2!r}
sys.path.insert(0, {TREE!r})
import gateway.run as R


class F:
    def __init__(self):
        self.returned = []


F._reply_to_is_trustworthy = R.GatewayRunner._reply_to_is_trustworthy
f = F()
print("KNOWN=" + repr(f._reply_to_is_trustworthy(
    {{"agent": "ash", "host": "will-ms-7b93.local"}}, {{"peer": "peer"}})))
print("UNKNOWN=" + repr(f._reply_to_is_trustworthy(
    {{"agent": "nobody-we-know", "host": "x"}}, {{"peer": "peer"}})))
'''
    probe_path = os.path.join(jail2, "probe.py")
    with open(probe_path, "w", encoding="utf-8") as fh:
        fh.write(probe_src)
    pr = _sp2.run([sys.executable, probe_path], capture_output=True,
                  text=True, timeout=180)
    out = pr.stdout
    check("G3 CONTROL: with a real identity.json, an UNKNOWN agent is REFUSED",
          "UNKNOWN=False" in out,
          f"stdout={out[-300:]!r} stderr={pr.stderr[-200:]!r} — the fix must "
          f"not be 'stop checking'")
    check("G4 NON-VACUITY: the KNOWN agent is still trusted in the same run",
          "KNOWN=True" in out,
          f"stdout={out[-300:]!r} — if both arms agree the predicate is inert")

    # ---- H: "NO POLICY AVAILABLE" IS NOT "POLICY SAYS YES" (Ash, #34 review)
    # In #32 the identity read was scoped to the host half, so a damaged file
    # disabled only the host comparison. Moving it above the known-peer check
    # with `return True` on failure widened the hole to the WHOLE predicate,
    # and H3/H4 reach it with NO file damage: `if known and ...` short-circuits
    # on a missing or empty peers map. Every G arm stays green throughout,
    # which is why only a differential probe finds it.
    #
    # THE DISCRIMINATING PAIR IS G3 vs H1, and it is Ash's: the SAME event and
    # the SAME predicate in two different homes must give OPPOSITE verdicts.
    # A single-home arm cannot tell "refuses correctly" from "refuses
    # everything", which is the vacuous control he caught in his own review.
    homes = {
        "H1 no identity.json at all": None,
        "H2 corrupt identity.json": "{ not json",
        "H3 valid json, NO peers key": '{"agent": "wren"}',
        "H4 valid json, EMPTY peers map": '{"agent": "wren", "peers": {}}',
    }
    for label, content in homes.items():
        hj = tempfile.mkdtemp(prefix="wren-nopolicy-")
        if content is not None:
            with open(os.path.join(hj, "identity.json"), "w", encoding="utf-8") as fh:
                fh.write(content)
        src = f'''import os, sys
os.environ["HERMES_HOME"] = {hj!r}
sys.path.insert(0, {TREE!r})
import gateway.run as R


class F:
    pass


F._reply_to_is_trustworthy = R.GatewayRunner._reply_to_is_trustworthy
print("V=" + repr(F()._reply_to_is_trustworthy(
    {{"agent": "nobody-we-know", "host": "x"}}, {{"peer": "peer"}})))
'''
        pp = os.path.join(hj, "p.py")
        with open(pp, "w", encoding="utf-8") as fh:
            fh.write(src)
        rr = _sp2.run([sys.executable, pp], capture_output=True, text=True,
                      timeout=180)
        check(f"{label} -> an unverifiable address is REFUSED",
              "V=False" in rr.stdout,
              f"stdout={rr.stdout.strip()!r} stderr={rr.stderr[-160:]!r} — "
              f"trusting here delivers a real reply somewhere nobody asked "
              f"for and closes the row saying it went home")

    # ---- I: THE HOST HALF, AND THE LOG DISTINCTION ASH ASKED FOR
    # Found by mutating my own subject after the H arms went green (round 4):
    #   34E  delete the host-mismatch branch entirely   -> SURVIVED
    #   34B  delete the "cannot verify" branch entirely -> SURVIVED
    # 34E is a plain coverage hole: the host comparison has been in this
    # predicate since #32 and NOTHING here ever asserted it, so a later edit
    # could delete it with every arm green. I1/I2 close it.
    #
    # 34B is subtler and is the more interesting of the two. Deleting the
    # unverifiable branch does not change any RETURN VALUE — an empty peers
    # map falls through to `agent not in known` and refuses anyway. It is
    # equivalent on the verdict and NOT equivalent on the thing Ash actually
    # asked for: "abstained because unverifiable" and "policy says no" must
    # not produce the same line, or the next person debugging cannot tell
    # which happened. I3 asserts the two refusals are DISTINGUISHABLE in the
    # log, which is the only place that distinction exists.
    probe2_src = f'''import logging, os, sys
os.environ["HERMES_HOME"] = {jail2!r}
sys.path.insert(0, {TREE!r})
logging.basicConfig(level=logging.ERROR, format="LOG:%(message)s")
import gateway.run as R


class F:
    pass


F._reply_to_is_trustworthy = R.GatewayRunner._reply_to_is_trustworthy
f = F()
print("HOSTBAD=" + repr(f._reply_to_is_trustworthy(
    {{"agent": "ash", "host": "attacker.example"}}, {{"peer": "peer"}})))
print("HOSTGOOD=" + repr(f._reply_to_is_trustworthy(
    {{"agent": "ash", "host": "will-ms-7b93.local"}}, {{"peer": "peer"}})))
print("UNKNOWN=" + repr(f._reply_to_is_trustworthy(
    {{"agent": "nobody-we-know", "host": "x"}}, {{"peer": "peer"}})))
'''
    pp2 = os.path.join(jail2, "probe_host.py")
    with open(pp2, "w", encoding="utf-8") as fh:
        fh.write(probe2_src)
    hr = _sp2.run([sys.executable, pp2], capture_output=True, text=True,
                  timeout=180)
    check("I1 a KNOWN agent declaring the WRONG HOST is REFUSED",
          "HOSTBAD=False" in hr.stdout,
          f"stdout={hr.stdout.strip()!r} stderr={hr.stderr[-200:]!r} — the "
          f"host comparison has been unasserted since #32; mutant 34E "
          f"deleted it and every other arm stayed green")
    check("I2 NON-VACUITY: the SAME agent with the RIGHT host is TRUSTED",
          "HOSTGOOD=True" in hr.stdout,
          f"stdout={hr.stdout.strip()!r} — without this I1 passes against a "
          f"predicate that refuses everything")

    # I3: the two refusals must not be the same line. Home with no policy
    # (H-shape) vs home with a policy that says no (G3-shape).
    nopolicy = tempfile.mkdtemp(prefix="wren-logdist-")
    with open(os.path.join(nopolicy, "identity.json"), "w", encoding="utf-8") as fh:
        fh.write('{"agent": "wren", "peers": {}}')
    probe3_src = f'''import logging, os, sys
os.environ["HERMES_HOME"] = {nopolicy!r}
sys.path.insert(0, {TREE!r})
logging.basicConfig(level=logging.ERROR, format="LOG:%(message)s")
import gateway.run as R


class F:
    pass


F._reply_to_is_trustworthy = R.GatewayRunner._reply_to_is_trustworthy
print("V=" + repr(F()._reply_to_is_trustworthy(
    {{"agent": "nobody-we-know", "host": "x"}}, {{"peer": "peer"}})))
'''
    pp3 = os.path.join(nopolicy, "p.py")
    with open(pp3, "w", encoding="utf-8") as fh:
        fh.write(probe3_src)
    nr = _sp2.run([sys.executable, pp3], capture_output=True, text=True,
                  timeout=180)
    nopolicy_log = "".join(
        l for l in (nr.stderr + nr.stdout).splitlines(True) if "LOG:" in l)
    knownhome_log = "".join(
        l for l in (hr.stderr + hr.stdout).splitlines(True)
        if "LOG:" in l and "known peer" in l)
    check("I3 'cannot verify' and 'not a known peer' are DIFFERENT log lines",
          ("cannot verify" in nopolicy_log
           and "known peer" in knownhome_log
           and "cannot verify" not in knownhome_log),
          f"nopolicy={nopolicy_log.strip()[:200]!r} "
          f"knownhome={knownhome_log.strip()[:200]!r} — both refuse, so the "
          f"verdict cannot tell them apart; the log is the only place the "
          f"distinction exists and Ash asked for it explicitly")

    check("H5 DISCRIMINATION: G3(known home)=False and H1(no home)=False are "
          "reached by DIFFERENT branches, and G4 proves the predicate still "
          "says True somewhere",
          "KNOWN=True" in out,
          "if the predicate refused everything, every H arm would pass "
          "vacuously; G4 is what rules that out")

    print()


asyncio.run(drive())

print()
if fails:
    print(f"  {len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print(f"  ALL PASS ({ran} cases)")
