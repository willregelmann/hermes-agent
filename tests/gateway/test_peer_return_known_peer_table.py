"""The known-peer authority is bot_peers; identity.json answers only "what host?".

WHY THIS FILE EXISTS — 118 real refusals on will-MS-7B93 between 2026-09-30
22:18:43Z and 2026-10-02 12:54:41Z (ash 63, refsdal 28, pair 27), every one
logging `(known: ['wren'])`. `_reply_to_is_trustworthy` resolved "is this a
known peer" against identity.json's `peers` map while every actual peer link
lives in config.yaml's `bot_peers`. The maps diverged — identity.json held one
name, bot_peers held four — and the return leg failed closed on every name the
older map never learned. Wren measured the same divergence on ha-pi (identity
peers ['ash'] vs bot_peers of five) with 6 refusals, 4 of them her own name.

TWO TABLES, TWO DIFFERENT KINDS OF FACT, AND THEY MUST NOT BE COLLAPSED:

  bot_peers[name].url  is a ROUTE — "how I reach you FROM HERE". Local,
                       topology-dependent, legitimately rewritten per box and
                       per profile.
  identity.json peers  is an IDENTITY — "who you say you are". A fact recorded
                       about the peer, which is what an anti-spoof check can
                       pin, because the declared host arrives from the network.

Collapsing them (deriving the expected host from the route's URL) looks clean
and is a latent misroute. Wren's counterexample, from PLAN-refsdal-draft13
step 7, which we both signed: inside the refsdal sandbox systemd-resolved
answers `will-MS-7B93.local` with a synthetic own-host record on the docker
bridge, so that profile's `bot_peers.ash.url` becomes `http://127.0.0.1:8642`
while ash still declares `host: will-MS-7B93.local`. A route-derived check
would compare 127.0.0.1 against will-MS-7B93.local and refuse 100% of returns
on that edge — recreating the bug this file closes, on an edge created later.
`test_route_host_may_differ_from_identity_host` pins exactly that.

So: bot_peers decides WHETHER a name is a peer; identity.json decides WHAT host
that peer should be claiming. A name in bot_peers with no recorded host is
still deliverable — the host arm simply has nothing to compare and says so.
"""

import json
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _runner(tmp_path, monkeypatch, *, bot_peers, identity, self_agent="ash"):
    """A real GatewayRunner method bound to a temp HERMES_HOME.

    Only the two data sources are faked, and they are faked as FILES/CONFIG the
    way production reads them — not by stubbing the reader, which would let a
    wrong reader pass.
    """
    import gateway.run_peer_completion as R

    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    if identity is not None:
        payload = {"agent": self_agent, "host": "will-MS-7B93.local"}
        payload.update(identity)
        (home / "identity.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    import hermes_cli.config as cfg

    monkeypatch.setattr(
        cfg, "load_config", lambda *a, **k: {"bot_peers": bot_peers}, raising=False
    )

    # The REAL methods off the mixin that defines them — not stubs, and not the
    # composed GatewayRunner, so the fixture cannot drag in gateway startup.
    # _own_agent_name is bound too: stubbing it would mean the self arm tests
    # the fixture's idea of identity instead of production's reader.
    cls = types.new_class("R", (object,), {})
    cls._reply_to_is_trustworthy = R.GatewayPeerCompletionMixin._reply_to_is_trustworthy
    cls._own_agent_name = R.GatewayPeerCompletionMixin._own_agent_name
    return cls()


# --- the 118-event bug -------------------------------------------------------


def test_peer_in_bot_peers_but_absent_from_identity_is_trusted(tmp_path, monkeypatch):
    """THE REGRESSION. refsdal/pair/ash were in bot_peers and not in identity."""
    r = _runner(
        tmp_path,
        monkeypatch,
        bot_peers={"refsdal": {"url": "http://will-MS-7B93.local:8642/p/physics"}},
        identity={"peers": {"wren": {"host": "ha-pi.local"}}},
    )
    assert r._reply_to_is_trustworthy(
        {"agent": "refsdal"}, {"peer": "refsdal"}
    ) is True


def test_unknown_name_in_neither_table_is_still_refused(tmp_path, monkeypatch):
    """Widening the authority must not make the check vacuous."""
    r = _runner(
        tmp_path,
        monkeypatch,
        bot_peers={"wren": {"url": "http://ha-pi.local:8642"}},
        identity={"peers": {"wren": {"host": "ha-pi.local"}}},
    )
    assert r._reply_to_is_trustworthy(
        {"agent": "stranger"}, {"peer": "stranger"}
    ) is False


def test_empty_bot_peers_refuses_rather_than_trusting(tmp_path, monkeypatch):
    """"No policy available" is not "policy says yes" (H1-H4 on #34)."""
    r = _runner(
        tmp_path,
        monkeypatch,
        bot_peers={},
        identity={"peers": {"wren": {"host": "ha-pi.local"}}},
    )
    assert r._reply_to_is_trustworthy({"agent": "wren"}, {"peer": "wren"}) is False


# --- route vs identity: Wren's counterexample --------------------------------


def test_route_host_may_differ_from_identity_host(tmp_path, monkeypatch):
    """PLAN-refsdal-draft13 step 7: the sandbox rewrites the ROUTE to loopback.

    The declared host still matches the recorded IDENTITY, so the return is
    trustworthy. A route-derived expectation would refuse this.
    """
    r = _runner(
        tmp_path,
        monkeypatch,
        bot_peers={"ash": {"url": "http://127.0.0.1:8642"}},
        identity={"peers": {"ash": {"host": "will-MS-7B93.local"}}},
        self_agent="refsdal",
    )
    assert r._reply_to_is_trustworthy(
        {"agent": "ash", "host": "will-MS-7B93.local"}, {"peer": "ash"}
    ) is True


def test_declared_host_contradicting_identity_is_refused(tmp_path, monkeypatch):
    """The anti-spoof arm survives: identity, not route, is the authority."""
    r = _runner(
        tmp_path,
        monkeypatch,
        bot_peers={"ash": {"url": "http://127.0.0.1:8642"}},
        identity={"peers": {"ash": {"host": "will-MS-7B93.local"}}},
        self_agent="refsdal",
    )
    assert r._reply_to_is_trustworthy(
        {"agent": "ash", "host": "evil.example"}, {"peer": "ash"}
    ) is False


def test_known_peer_on_box_with_no_identity_knowledge_is_deliverable(
    tmp_path, monkeypatch
):
    """NO IDENTITY KNOWLEDGE ON THIS BOX -> HOST ARM INERT.

    Wren's ruling. An EMPTY peers map and a MISSING file assert the same
    proposition — this box records no identity hosts — so no rule may
    distinguish them. `physics` on will-MS-7B93 has an empty map and `pair`
    has no file; keying off the file would admit Foil and refuse Refsdal.
    """
    r = _runner(
        tmp_path,
        monkeypatch,
        bot_peers={"money": {"url": "http://ha-pi.local:8642/p/money"}},
        identity={"peers": {}},
    )
    assert r._reply_to_is_trustworthy(
        {"agent": "money", "host": "ha-pi.local"}, {"peer": "money"}
    ) is True


def test_missing_identity_file_is_the_same_proposition_as_an_empty_map(
    tmp_path, monkeypatch
):
    """pair/Foil has NO identity.json; same verdict as the empty-map case."""
    r = _runner(
        tmp_path,
        monkeypatch,
        bot_peers={"pair": {"url": "http://will-MS-7B93.local:8643"}},
        identity=None,
    )
    assert r._reply_to_is_trustworthy({"agent": "pair"}, {"peer": "pair"}) is True


def test_identity_known_for_others_but_not_this_peer_is_still_inert(
    tmp_path, monkeypatch
):
    """PER-PEER, NOT PER-BOX. Wren's ruling, after correcting herself and me.

    THIS TEST ASSERTED THE OPPOSITE UNTIL 2026-10-02 22:11Z, and it is being
    inverted because the RULE changed, not because the implementation did. Her
    first formulation in 3e137eed was "if identity.json records at least one
    peer host AND this peer is not among them -> REFUSE"; she replaced it in
    that same handoff with the per-peer rule, and confirmed the per-peer
    reading in 43182ffa when my per-box implementation left a different test
    red. The measured argument for the change: all 118 refusals on
    will-MS-7B93 landed on the `default` profile, which records a host for
    `wren` only, so a per-box rule leaves refsdal (28) and pair (27) refused —
    55 of 118 survive. A host recorded for one peer is not evidence about
    another.
    """
    r = _runner(
        tmp_path,
        monkeypatch,
        bot_peers={
            "wren": {"url": "http://ha-pi.local:8642"},
            "money": {"url": "http://ha-pi.local:8642/p/money"},
        },
        identity={"peers": {"wren": {"host": "ha-pi.local"}}},
    )
    # money is a known peer with no recorded host -> arm inert -> delivered.
    assert r._reply_to_is_trustworthy(
        {"agent": "money", "host": "ha-pi.local"}, {"peer": "money"}
    ) is True
    # ...while wren's arm stays LIVE on the same box, which is the whole point
    # of per-peer: knowledge about one peer is neither extended to nor
    # withheld from another.
    assert r._reply_to_is_trustworthy(
        {"agent": "wren", "host": "evil.example"}, {"peer": "wren"}
    ) is False
    assert r._reply_to_is_trustworthy(
        {"agent": "wren", "host": "ha-pi.local"}, {"peer": "wren"}
    ) is True


# --- the self case: 63 of 118 ------------------------------------------------


def test_self_named_return_address_is_not_a_refusal(tmp_path, monkeypatch):
    """reply_to naming THIS box's own agent is local delivery, not an error.

    63 of the 118 refusals were `reply_to.agent='ash'` refused on Ash's own
    box, and 4 of Wren's 6 were 'wren' on hers. Self is correctly absent from
    one's own peer table; that absence is not a spoof.
    """
    r = _runner(
        tmp_path,
        monkeypatch,
        bot_peers={"wren": {"url": "http://ha-pi.local:8642"}},
        identity={"peers": {"wren": {"host": "ha-pi.local"}}},
        self_agent="ash",
    )
    assert r._reply_to_is_trustworthy({"agent": "ash"}, {"peer": "peer"}) is True


# --- Wren's gate on the future hook -----------------------------------------


def test_published_shape_round_trips_and_url_form_does_not(tmp_path, monkeypatch):
    """Wren's required gate for the in-chat-reply hook.

    Whatever a publisher puts in `reply_to` must survive this validator. The
    {agent, host} form does; the URL form must NOT silently look fine, or the
    hook ships and the validator refuses it — the same failure one layer up.
    """
    r = _runner(
        tmp_path,
        monkeypatch,
        bot_peers={"wren": {"url": "http://ha-pi.local:8642"}},
        identity={"peers": {"wren": {"host": "ha-pi.local"}}},
    )
    good = {"agent": "wren", "host": "ha-pi.local"}
    assert r._reply_to_is_trustworthy(good, {"peer": "wren"}) is True

    # NEGATIVE ARM: a URL where a host belongs is not a host.
    bad = {"agent": "wren", "host": "http://ha-pi.local:8642"}
    assert r._reply_to_is_trustworthy(bad, {"peer": "wren"}) is False


def test_the_four_outcomes_have_four_distinguishable_messages(
    tmp_path, monkeypatch, caplog
):
    """Wren's I3 requirement, widened: the new design has FOUR outcomes.

    118 refusal lines could not tell us which case we were in. Each outcome
    must be identifiable from its log line alone, so a reader of the logs can
    distinguish "not a peer" from "no policy" from "spoof/stale" from
    "nothing to compare".
    """
    import logging

    seen = {}

    def grab(key, runner, reply_to, evt):
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            verdict = runner._reply_to_is_trustworthy(reply_to, evt)
        seen[key] = (
            verdict,
            " ".join(rec.getMessage() for rec in caplog.records),
        )

    # (1) in neither table -> refuse
    grab(
        "not_a_peer",
        _runner(
            tmp_path / "a",
            monkeypatch,
            bot_peers={"wren": {"url": "http://ha-pi.local:8642"}},
            identity={"peers": {"wren": {"host": "ha-pi.local"}}},
        ),
        {"agent": "stranger"},
        {"peer": "stranger"},
    )
    # (2) no policy at all -> refuse (the H1-H4 invariant's new home)
    grab(
        "no_policy",
        _runner(tmp_path / "b", monkeypatch, bot_peers={}, identity={"peers": {}}),
        {"agent": "wren"},
        {"peer": "wren"},
    )
    # (3a) declared host contradicts the recorded identity -> refuse
    grab(
        "host_conflict",
        _runner(
            tmp_path / "c",
            monkeypatch,
            bot_peers={"ash": {"url": "http://127.0.0.1:8642"}},
            identity={"peers": {"ash": {"host": "will-MS-7B93.local"}}},
            self_agent="refsdal",
        ),
        {"agent": "ash", "host": "evil.example"},
        {"peer": "ash"},
    )
    # (4) known peer, no identity knowledge on this box -> deliver + WARNING
    grab(
        "arm_inert",
        _runner(
            tmp_path / "d",
            monkeypatch,
            bot_peers={"pair": {"url": "http://will-MS-7B93.local:8643"}},
            identity=None,
        ),
        {"agent": "pair", "host": "will-MS-7B93.local"},
        {"peer": "pair"},
    )

    assert seen["not_a_peer"][0] is False
    assert seen["no_policy"][0] is False
    assert seen["host_conflict"][0] is False
    assert seen["arm_inert"][0] is True

    # THE POINT: four DIFFERENT messages, not one message four times.
    msgs = {k: v[1] for k, v in seen.items()}
    assert "not a known peer" in msgs["not_a_peer"]
    assert "no peer table" in msgs["no_policy"]
    assert "knows" in msgs["host_conflict"] and "route" in msgs["host_conflict"]
    assert "host arm inert" in msgs["arm_inert"]
    assert len({m.split(" — ")[0] for m in msgs.values()}) == 4


def test_divergence_between_tables_is_logged_at_error(tmp_path, monkeypatch, caplog):
    """Wren's refinement: one line distinguishing stale / spoof / topology.

    None of today's 118 lines could tell those apart. The ERROR must name the
    declared host, the recorded host AND the route.
    """
    import logging

    r = _runner(
        tmp_path,
        monkeypatch,
        bot_peers={"ash": {"url": "http://127.0.0.1:8642"}},
        identity={"peers": {"ash": {"host": "will-MS-7B93.local"}}},
        self_agent="refsdal",
    )
    with caplog.at_level(logging.ERROR):
        assert r._reply_to_is_trustworthy(
            {"agent": "ash", "host": "evil.example"}, {"peer": "ash"}
        ) is False
    blob = " ".join(rec.getMessage() for rec in caplog.records).casefold()
    assert "evil.example" in blob
    assert "will-ms-7b93.local" in blob
    assert "127.0.0.1" in blob


def test_identity_host_for_a_peer_absent_from_bot_peers_refuses_at_outcome_1(
    tmp_path, monkeypatch, caplog
):
    """Wren's review target (a), handoff 497206e0: order in code, not prose.

    bot_peers is the known-peer AUTHORITY. So a peer that identity.json
    records a host for, but which bot_peers does not list at all, must be
    refused as an unknown peer BEFORE the host arm is ever consulted —
    otherwise identity.json could smuggle a name past the authority by
    recording a host for it.

    This pins the ORDER of the two checks, which is why it asserts on the
    outcome-1 phrase rather than only on the False: both branches refuse,
    so the verdict alone cannot tell them apart and would pass even if the
    checks were swapped.
    """
    import logging

    r = _runner(
        tmp_path,
        monkeypatch,
        bot_peers={"wren": {"url": "http://ha-pi.local:8642"}},
        identity={
            "peers": {
                "ghost": {"host": "ghost.local"},
                "wren": {"host": "ha-pi.local"},
            }
        },
        self_agent="ash",
    )
    with caplog.at_level(logging.WARNING):
        assert r._reply_to_is_trustworthy(
            {"agent": "ghost", "host": "ghost.local"}, {"peer": "ghost"}
        ) is False
    blob = " ".join(rec.getMessage() for rec in caplog.records)
    assert "not a known peer on this box" in blob
    # And NOT via the host arm, whose messages name the recorded host.
    assert "but this box knows" not in blob


def test_declared_host_withheld_while_known_is_refused_distinctly(
    tmp_path, monkeypatch, caplog
):
    """Wren's defect, handoff 3a37322d: WITHHOLDING is not CONTRADICTING.

    Before outcome 3b existed, `if declared_host and declared_host != known`
    short-circuited on an omitted/blank host and fell through to `return
    True` — delivered with NO log at any level. That made an absent
    attacker-controlled field strictly QUIETER than outcome 4, where the box
    knows nothing and still warns.

    Three shapes of "declined to say" (omitted key, empty string,
    whitespace) must all refuse, and the message must be distinguishable
    from 3a so a reader can tell a spoof from a withheld field.
    """
    import logging

    for label, reply_to in (
        ("omitted", {"agent": "wren"}),
        ("empty", {"agent": "wren", "host": ""}),
        ("whitespace", {"agent": "wren", "host": "   "}),
    ):
        r = _runner(
            tmp_path,
            monkeypatch,
            bot_peers={"wren": {"url": "http://ha-pi.local:8642"}},
            identity={"peers": {"wren": {"host": "ha-pi.local"}}},
            self_agent="ash",
        )
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert r._reply_to_is_trustworthy(
                reply_to, {"peer": "wren"}
            ) is False, label
        blob = " ".join(rec.getMessage() for rec in caplog.records)
        # Refused, and LOUDLY — the silent-delivery path is what this closes.
        assert "declared NO host" in blob, label
        assert "ha-pi.local" in blob, label
        # Distinct from 3a, whose message quotes the contradicting value.
        assert "declared host" not in blob.replace("declared NO host", ""), label


def test_withholding_refuses_only_when_a_host_is_actually_recorded(
    tmp_path, monkeypatch, caplog
):
    """The 3b arm must not swallow outcome 4.

    A peer this box records NO host for still delivers on a missing host
    field — the box has nothing to compare against, which is a different
    proposition from withholding. Without this, 3b would re-refuse the very
    population the per-peer rule exists to deliver: all 118 real refusals
    declared ash/refsdal/pair, none of which has a recorded host here.
    """
    import logging

    r = _runner(
        tmp_path,
        monkeypatch,
        bot_peers={"refsdal": {"url": "http://127.0.0.1:8644"}},
        identity={"peers": {"wren": {"host": "ha-pi.local"}}},
        self_agent="ash",
    )
    with caplog.at_level(logging.WARNING):
        assert r._reply_to_is_trustworthy(
            {"agent": "refsdal"}, {"peer": "refsdal"}
        ) is True
    blob = " ".join(rec.getMessage() for rec in caplog.records)
    assert "records no identity host" in blob
    assert "declared NO host" not in blob


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
