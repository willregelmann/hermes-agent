"""Gateway startup gives every served profile a signing keypair (``agent/profile_keypair.py``)."""

from pathlib import Path

from agent.profile_keypair import ensure_keypair, private_key_path, public_key_pem
from gateway.config import GatewayConfig
from gateway.run import GatewayRunner


def _runner(tmp_path, monkeypatch, *, multiplex):
    home = tmp_path / ".hermes"
    for name in ("alpha", "beta"):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("model: {default: m}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=multiplex)
    return runner, home


def test_multiplex_startup_creates_missing_keypairs_and_keeps_existing(tmp_path, monkeypatch):
    runner, home = _runner(tmp_path, monkeypatch, multiplex=True)
    alpha, beta = home / "profiles" / "alpha", home / "profiles" / "beta"
    ensure_keypair(alpha)
    alpha_before = public_key_pem(alpha)

    runner._ensure_served_profile_keypairs()

    assert public_key_pem(alpha) == alpha_before
    assert len({public_key_pem(h) for h in (home, alpha, beta)}) == 3


def test_single_profile_startup_only_touches_the_launch_profile(tmp_path, monkeypatch):
    runner, home = _runner(tmp_path, monkeypatch, multiplex=False)

    runner._ensure_served_profile_keypairs()

    assert private_key_path(home).exists()
    assert not private_key_path(home / "profiles" / "alpha").exists()
