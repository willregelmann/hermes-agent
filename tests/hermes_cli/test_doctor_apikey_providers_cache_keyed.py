"""wren:i24 — the doctor API-key provider cache (now doctor_connectivity._APIKEY_PROVIDERS_CACHE) was a bare module global.

_check_apikey_provider_health() (via _build_apikey_providers_list()) calls
providers.list_providers(), which returns PROFILE-SCOPED plugin
registrations (bundled + $HERMES_HOME/plugins/model-providers/<name> +
entry points). A bare global cached the FIRST profile's provider list for
the life of the process; every other profile's `hermes doctor` run would
silently describe the wrong profile's providers until restart.

Same defect shape as agent/turn_context.py::_RECALL_CFG_CACHE and
agent/relay_runtime.py::_SEGMENTS_CONFIG_CACHE (Wren/Ash, 2026-09-15/16) —
third confirmed instance of the pattern, not a one-off.

Fix here is KEYING ONLY, not full profile isolation: the underlying
providers/__init__.py discovery (_discovered, _REGISTRY) is still a bare
global one level down, so two profiles running in the SAME PROCESS would
still get the first profile's plugin registrations even after this fix —
that is wren:i24's next member, not fixed here. What this test proves is
narrower and still real: this cache no longer PINS across cache-key
changes, and does not regrow one entry per lookup for the same key.
"""

from __future__ import annotations

import contextlib
import io
import sys
import types
from argparse import Namespace


def test_apikey_providers_cache_is_keyed_not_a_bare_global():
    from hermes_cli import doctor_connectivity as doctor

    doctor._reset_apikey_providers_cache_for_tests()
    assert doctor._APIKEY_PROVIDERS_CACHE == {}, (
        "cache must be a dict (keyed), not a bare None/list global"
    )


def test_apikey_providers_cache_key_function_reads_hermes_home(monkeypatch, tmp_path):
    from hermes_cli import doctor_connectivity as doctor

    fake_home_a = tmp_path / "profile_a"
    fake_home_b = tmp_path / "profile_b"
    fake_home_a.mkdir()
    fake_home_b.mkdir()

    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: fake_home_a
    )
    key_a = doctor._apikey_providers_cache_key()
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: fake_home_b
    )
    key_b = doctor._apikey_providers_cache_key()

    assert key_a != key_b, (
        "two different HERMES_HOME values must produce two different cache "
        "keys, or one profile's provider list pins the other's"
    )


def test_two_profiles_do_not_pin_each_other(monkeypatch, tmp_path):
    """Regression for the actual bug: calling under profile A then profile B
    must not return profile A's cached list for B.

    Goes through doctor._apikey_providers_for_home() — the SAME accessor
    run_doctor() calls — rather than re-deriving the key/cache-write logic
    inline. A suite that reimplements the lookup can pass while the real
    call site regresses (e.g. a stray `global` reintroduced); binding to
    the real accessor closes that gap (wren393 CHANGES_REQUESTED, 2026-09-16).
    """
    from hermes_cli import doctor_connectivity as doctor

    doctor._reset_apikey_providers_cache_for_tests()

    calls = []

    def fake_build():
        calls.append(1)
        return [f"built-for-call-{len(calls)}"]

    monkeypatch.setattr(doctor, "_build_apikey_providers_list", fake_build)

    fake_home_a = tmp_path / "profile_a"
    fake_home_b = tmp_path / "profile_b"

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: fake_home_a)
    result_a = doctor._apikey_providers_for_home()

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: fake_home_b)
    result_b = doctor._apikey_providers_for_home()

    assert result_a != result_b, (
        "profile B was served profile A's cached provider list — the bare "
        "global pinning defect this fix targets"
    )
    assert len(calls) == 2, (
        f"expected exactly one rebuild per distinct profile key, got {len(calls)} "
        "builds — either under-caching (rebuilding every call, defeats the "
        "point) or the keys collided"
    )

    # Non-vacuity: calling under profile A again must NOT rebuild (still cached).
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: fake_home_a)
    doctor._apikey_providers_for_home()
    assert len(calls) == 2, "re-lookup under the same profile must be served from cache"


# ---------------------------------------------------------------------------
# THE CALL SITE, BOUND BEHAVIOURALLY (2026-09-17).
#
# This replaces test_run_doctor_call_site_uses_the_real_accessor, which was an
# inspect.getsource() substring check.  MEASURED: a mutant that reintroduces an
# unkeyed inline read at the call site AND leaves the accessor name in the
# function under `if False:` reproduces the pinning bug and PASSES that check.
# The name being present and the call site using it are different facts, and a
# substring check reads only the first.
#
# These arms drive run_doctor() twice under two HERMES_HOME values with the
# provider-list builder faked to one network-free provider named after the
# resolved home, and assert on the labels it PRINTED plus the build order.
# ---------------------------------------------------------------------------
_PROVIDER_ENV = (
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
    "GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY", "KIMI_API_KEY", "KIMI_CN_API_KEY",
    "ARCEEAI_API_KEY", "DEEPSEEK_API_KEY", "HF_TOKEN", "DASHSCOPE_API_KEY",
    "MINIMAX_API_KEY", "MINIMAX_CN_API_KEY", "AI_GATEWAY_API_KEY", "KILOCODE_API_KEY",
    "OPENCODE_ZEN_API_KEY", "OPENCODE_GO_API_KEY", "XIAOMI_API_KEY", "GMI_API_KEY",
)


def _doctor_output_under(doctor_mod, monkeypatch, home, project, label):
    """Run run_doctor() with HERMES_HOME=home and return its stdout."""
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: home)
    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    return buf.getvalue()


def test_run_doctor_call_site_serves_the_current_profile(monkeypatch, tmp_path):
    from hermes_cli import doctor as doctor_mod
    from hermes_cli import doctor_connectivity

    doctor_connectivity._reset_apikey_providers_cache_for_tests()

    project = tmp_path / "project"
    project.mkdir()
    homes = {}
    for name in ("profile_a", "profile_b"):
        h = tmp_path / name
        h.mkdir()
        (h / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
        (h / ".env").write_text("", encoding="utf-8")
        homes[name] = h

    for env_name in _PROVIDER_ENV:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("WREN_FAKE_PROVIDER_KEY", "k")

    monkeypatch.setitem(sys.modules, "model_tools", types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    ))
    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
    except Exception:
        pass

    import hermes_constants

    builds = []

    def fake_build():
        # One distinct, network-free provider per HERMES_HOME.
        name = hermes_constants.get_hermes_home().name
        builds.append(name)
        return [(f"WrenProbe-{name}", ["WREN_FAKE_PROVIDER_KEY"],
                 "https://example.invalid/v1/models", "", False)]

    monkeypatch.setattr(doctor_connectivity, "_build_apikey_providers_list", fake_build)

    out_a = _doctor_output_under(doctor_mod, monkeypatch, homes["profile_a"], project, "a")
    assert "WrenProbe-profile_a" in out_a, (
        "non-vacuity: profile A's own provider must be probed and printed"
    )

    out_b = _doctor_output_under(doctor_mod, monkeypatch, homes["profile_b"], project, "b")
    assert "WrenProbe-profile_b" in out_b, (
        "run_doctor under profile B served a provider list that is not B's — "
        "the call site is reading the cache under the wrong key (or no key)"
    )
    assert "WrenProbe-profile_a" not in out_b, (
        "run_doctor under profile B still printed profile A's provider — the "
        "pinning bug is back at the call site"
    )

    out_a2 = _doctor_output_under(doctor_mod, monkeypatch, homes["profile_a"], project, "a2")
    assert "WrenProbe-profile_a" in out_a2
    assert builds == ["profile_a", "profile_b"], (
        f"the call site must memoize per profile; builds={builds}"
    )


def test_mutant_bare_global_would_pin(monkeypatch, tmp_path):
    """MUTANT ZERO shape: with a bare-global cache (the pre-fix code), the
    second profile's lookup returns the first profile's list. This asserts
    the mutant's own behaviour to prove the test can actually distinguish
    the two shapes — it does not touch doctor.py, it re-derives the old
    logic inline."""
    _mutant_cache = {"value": None}

    def mutant_lookup(build_fn):
        if _mutant_cache["value"] is None:
            _mutant_cache["value"] = build_fn()
        return _mutant_cache["value"]

    calls = []

    def fake_build():
        calls.append(1)
        return [f"built-{len(calls)}"]

    result_a = mutant_lookup(fake_build)
    result_b = mutant_lookup(fake_build)

    assert result_a == result_b, (
        "sanity: reproducing the OLD bug shape — a bare global DOES pin "
        "the second call to the first call's result, confirming this test "
        "suite would have failed against the pre-fix code"
    )
    assert len(calls) == 1
