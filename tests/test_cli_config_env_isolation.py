"""``load_cli_config()`` must be a PURE read of the current hermes home.

Since #19 the loader resolves ``get_hermes_home()`` on every call, which is what
makes it correct to call from inside a profile's home scope (the multiplexed
gateway does exactly that via lazy imports).  But the loader also used to bridge
~20 resolved values into process-global ``os.environ`` -- ``TERMINAL_*``,
``BROWSER_INACTIVITY_TIMEOUT``, ``AUXILIARY_*_MODEL``, ``HERMES_REDACT_SECRETS``,
``HERMES_CJK_FTS``, ``HERMES_SEARCH_SLOW_MS``.  Combined, those two properties
mean a single read under profile B rewrites the whole process's environment with
B's settings, and the writes PERSIST after the scope exits: a cross-profile
config leak.  #20 moved the bridge into ``_export_config_to_env()``, called once
at import.

This suite exercises the real path in a real child interpreter with two real
hermes homes and the real ``set_hermes_home_override`` API -- no mocks, because
the subject IS which file gets read and which environment gets written.
"""
import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])

WATCHED = (
    "HERMES_CJK_FTS",
    "HERMES_SEARCH_SLOW_MS",
    "HERMES_REDACT_SECRETS",
    "TERMINAL_TIMEOUT",
    "BROWSER_INACTIVITY_TIMEOUT",
)

CFG_A = """
model:
  provider: openai
  model: MODEL-FROM-A
sessions:
  cjk_fts: false
  search_slow_ms: 1111
security:
  redact_secrets: false
terminal:
  timeout: 111
browser:
  inactivity_timeout: 11
"""

CFG_B = """
model:
  provider: openai
  model: MODEL-FROM-B
sessions:
  cjk_fts: true
  search_slow_ms: 9999
security:
  redact_secrets: true
terminal:
  timeout: 999
  ssh_host: host-from-B
  docker_image: image-from-B
browser:
  inactivity_timeout: 99
"""

CHILD = textwrap.dedent(
    """
    import importlib, json, os, sys
    sys.path.insert(0, sys.argv[1])
    HOME_B = sys.argv[2]
    WATCHED = %r

    cli = importlib.import_module("cli")
    import hermes_constants as hc

    def snap():
        # The WHOLE environment, not a watchlist.  A pure read must write NO
        # key; naming five of ~35 bridged vars makes the arm blind to the
        # other thirty (measured: a mutant leaking TERMINAL_SSH_HOST /
        # TERMINAL_DOCKER_IMAGE / TERMINAL_ENV survived the watchlist arm).
        return dict(os.environ)

    out = {"home_A": str(hc.get_hermes_home()), "env_after_import": snap()}
    out["cfgA_slow"] = (cli.CLI_CONFIG.get("sessions") or {}).get("search_slow_ms")

    tok = hc.set_hermes_home_override(HOME_B)
    try:
        out["home_B_seen"] = str(hc.get_hermes_home())
        out["env_before_B_call"] = snap()
        cfgB = cli.load_cli_config()
        out["env_after_B_call"] = snap()
        out["cfgB_slow"] = (cfgB.get("sessions") or {}).get("search_slow_ms")
        out["cfgB_terminal_timeout"] = (cfgB.get("terminal") or {}).get("timeout")
        # The bridge must still WORK when called on purpose -- it moved, it did
        # not disappear.  Tolerate its absence here so that running this suite
        # against PRE-FIX cli.py fails on the leak assertion (the real finding)
        # instead of erroring out before it is reached.
        exporter = getattr(cli, "_export_config_to_env", None)
        if exporter is None:
            out["exporter_present"] = False
        else:
            out["exporter_present"] = True
            out["env_before_explicit_export"] = snap()
            exporter(cfgB, True)
            out["env_after_explicit_export"] = snap()
    finally:
        hc.reset_hermes_home_override(tok)
    out["env_after_scope_exit"] = snap()
    print("@@RESULT@@" + json.dumps(out))
    """
) % (WATCHED,)


def _child_python():
    """An interpreter that can actually import ``cli``.

    In CI that is ``sys.executable``.  On a dev box the test runner may live in
    a lean venv without the CLI's runtime deps (rich, prompt_toolkit...), so fall
    back to the installed Hermes venv.  The child is a REAL interpreter on
    purpose: the subject of this suite is import-time and process-global state,
    which cannot be observed in-process.
    """
    candidates = [sys.executable]
    hermes_venv = pathlib.Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    if hermes_venv.exists():
        candidates.append(str(hermes_venv))
    for exe in candidates:
        probe = subprocess.run(
            [exe, "-c", "import sys; sys.path.insert(0, %r); import cli" % REPO_ROOT],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={**os.environ, "HERMES_QUIET": "1"},
            timeout=300,
        )
        if probe.returncode == 0:
            return exe
    pytest.skip("no interpreter available that can import cli (missing runtime deps)")


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    td = tmp_path_factory.mktemp("cli_config_env_isolation")
    home_a = td / "homeA"
    home_b = td / "homeB"
    home_a.mkdir()
    home_b.mkdir()
    (home_a / "config.yaml").write_text(CFG_A)
    (home_b / "config.yaml").write_text(CFG_B)

    env = dict(os.environ)
    env["HERMES_HOME"] = str(home_a)
    env["HERMES_QUIET"] = "1"
    for key in WATCHED:
        env.pop(key, None)

    proc = subprocess.run(
        [_child_python(), "-c", CHILD, REPO_ROOT, str(home_b)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=300,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("@@RESULT@@"):
            return json.loads(line[len("@@RESULT@@") :])
    pytest.fail(
        "child interpreter produced no result (rc=%s)\nstdout:\n%s\nstderr:\n%s"
        % (proc.returncode, proc.stdout[-2000:], proc.stderr[-3000:])
    )


def test_non_vacuity_import_time_export_still_happens(probe):
    """The import-time bridge is the one caller that MUST still write env.

    Without this arm, deleting the bridge outright would make every other test
    here pass while silently breaking terminal_tool / session search config.
    It also proves the child really loaded home A, so "env did not change" in
    the leak tests below cannot be an artefact of nothing having been loaded.
    """
    assert probe["home_A"].endswith("homeA")
    assert probe["cfgA_slow"] == 1111
    env = probe["env_after_import"]
    assert env["HERMES_SEARCH_SLOW_MS"] == "1111"
    assert env["HERMES_CJK_FTS"] == "False"
    assert env["HERMES_REDACT_SECRETS"] == "false"
    assert env["TERMINAL_TIMEOUT"] == "111"
    assert env["BROWSER_INACTIVITY_TIMEOUT"] == "11"


def test_load_follows_the_profile_home_override(probe):
    """#19's property, guarded here so the #20 fix cannot be "fixed" by refreezing."""
    assert probe["home_B_seen"].endswith("homeB")
    assert probe["cfgB_slow"] == 9999
    assert probe["cfgB_terminal_timeout"] == 999


def test_reading_config_under_another_profile_does_not_write_process_env(probe):
    """The regression. A read is a read -- of EVERY key, not of a watchlist.

    The population matters more than the assertion here.  An earlier version of
    this arm compared five named vars; a mutant that re-leaked only
    ``TERMINAL_SSH_HOST`` / ``TERMINAL_DOCKER_IMAGE`` / ``TERMINAL_ENV`` --
    none of them in that list, all of them written by the same bridge --
    survived the whole suite.  Diff the entire environment instead: the claim
    being made is "writes nothing", and that claim has no watchlist.
    """
    before = probe["env_before_B_call"]
    after = probe["env_after_B_call"]
    keys = set(before) | set(after)
    leaked = {
        k: (before.get(k), after.get(k))
        for k in keys
        if before.get(k) != after.get(k)
    }
    assert leaked == {}, "load_cli_config() exported profile B's config: %r" % (leaked,)


def test_scope_exit_leaves_the_environment_exactly_as_import_left_it(probe):
    """The persistence half: leaks that survive the scope are the damaging ones.

    Same whole-environment population as above, over the widest possible span:
    import time to after ``reset_hermes_home_override``.  Note the explicit
    ``_export_config_to_env(cfgB, ...)`` call in between is DELIBERATE and does
    write B's values, so this arm compares against ``env_after_explicit_export``
    -- it asserts that nothing ELSE moved, and in particular that scope exit
    itself neither writes nor restores.
    """
    after_export = probe["env_after_explicit_export"]
    at_exit = probe["env_after_scope_exit"]
    keys = set(after_export) | set(at_exit)
    moved = {
        k: (after_export.get(k), at_exit.get(k))
        for k in keys
        if after_export.get(k) != at_exit.get(k)
    }
    assert moved == {}, "environment moved across scope exit: %r" % (moved,)


def test_explicit_export_covers_the_whole_terminal_mapping(probe):
    """Population arm for the exporter itself.

    ``_export_config_to_env`` bridges ~35 keys through one ``env_mappings``
    loop.  Asserting three of them cannot distinguish "the loop ran" from "the
    loop ran once and broke".  Assert on the SET of keys the export introduces
    or changes, which is derived from the environment diff rather than from a
    list written by hand here.
    """
    before = probe["env_before_explicit_export"]
    after = probe["env_after_explicit_export"]
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    # B's config sets terminal.timeout, browser.inactivity_timeout, redact,
    # cjk_fts and search_slow_ms; the terminal loop additionally re-asserts the
    # resolved env_type/degraded_mode/cwd defaults.
    assert "TERMINAL_TIMEOUT" in changed
    assert "HERMES_SEARCH_SLOW_MS" in changed
    assert "HERMES_REDACT_SECRETS" in changed
    assert "HERMES_CJK_FTS" in changed
    assert "BROWSER_INACTIVITY_TIMEOUT" in changed
    # ...and the mapping loop must reach past its first entry.
    terminal_keys = {k for k in changed if k.startswith("TERMINAL_")}
    assert terminal_keys >= {"TERMINAL_TIMEOUT", "TERMINAL_SSH_HOST", "TERMINAL_DOCKER_IMAGE"}, (
        "the terminal env_mappings loop bridged only %r -- it did not run to "
        "completion (B's config sets timeout, ssh_host and docker_image, which "
        "sit at different positions in env_mappings)" % (sorted(terminal_keys),)
    )


def test_export_helper_still_bridges_when_called_explicitly(probe):
    """Moved, not removed: the explicit call is what import time uses."""
    assert probe["exporter_present"], "cli._export_config_to_env is missing"
    env = probe["env_after_explicit_export"]
    assert env["HERMES_SEARCH_SLOW_MS"] == "9999"
    assert env["TERMINAL_TIMEOUT"] == "999"
    assert env["HERMES_REDACT_SECRETS"] == "true"
