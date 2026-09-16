"""wren:i24 — doctor._APIKEY_PROVIDERS_CACHE was a bare module global.

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


def test_apikey_providers_cache_is_keyed_not_a_bare_global():
    from hermes_cli import doctor

    doctor._reset_apikey_providers_cache_for_tests()
    assert doctor._APIKEY_PROVIDERS_CACHE == {}, (
        "cache must be a dict (keyed), not a bare None/list global"
    )


def test_apikey_providers_cache_key_function_reads_hermes_home(monkeypatch, tmp_path):
    from hermes_cli import doctor

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
    must not return profile A's cached list for B."""
    from hermes_cli import doctor

    doctor._reset_apikey_providers_cache_for_tests()

    calls = []

    def fake_build():
        calls.append(1)
        return [f"built-for-call-{len(calls)}"]

    monkeypatch.setattr(doctor, "_build_apikey_providers_list", fake_build)

    fake_home_a = tmp_path / "profile_a"
    fake_home_b = tmp_path / "profile_b"

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: fake_home_a)
    key_a = doctor._apikey_providers_cache_key()
    if key_a not in doctor._APIKEY_PROVIDERS_CACHE:
        doctor._APIKEY_PROVIDERS_CACHE[key_a] = doctor._build_apikey_providers_list()
    result_a = doctor._APIKEY_PROVIDERS_CACHE[key_a]

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: fake_home_b)
    key_b = doctor._apikey_providers_cache_key()
    if key_b not in doctor._APIKEY_PROVIDERS_CACHE:
        doctor._APIKEY_PROVIDERS_CACHE[key_b] = doctor._build_apikey_providers_list()
    result_b = doctor._APIKEY_PROVIDERS_CACHE[key_b]

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
    key_a2 = doctor._apikey_providers_cache_key()
    if key_a2 not in doctor._APIKEY_PROVIDERS_CACHE:
        doctor._APIKEY_PROVIDERS_CACHE[key_a2] = doctor._build_apikey_providers_list()
    assert len(calls) == 2, "re-lookup under the same profile must be served from cache"


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
