"""Tests for auxiliary model config bridging — verifies that config.yaml values
are properly mapped to environment variables by both CLI and gateway loaders.

Also tests the vision_tools and browser_tool model override env vars.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _run_auxiliary_bridge(config_dict, monkeypatch):
    """Simulate the auxiliary config → env var bridging logic shared by CLI and gateway.

    This mirrors the code in cli.py load_cli_config() and gateway/run.py.
    Both use the same pattern; we test it once here.
    """
    # Clear env vars
    for key in (
        "AUXILIARY_VISION_PROVIDER", "AUXILIARY_VISION_MODEL",
        "AUXILIARY_VISION_BASE_URL",
        "AUXILIARY_APPROVAL_PROVIDER", "AUXILIARY_APPROVAL_MODEL",
        "AUXILIARY_APPROVAL_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)

    # Compression config is read directly from config.yaml — no env var bridging.

    # Auxiliary bridge
    auxiliary_cfg = config_dict.get("auxiliary", {})
    if auxiliary_cfg and isinstance(auxiliary_cfg, dict):
        aux_task_env = {
            "vision": {
                "provider": "AUXILIARY_VISION_PROVIDER",
                "model": "AUXILIARY_VISION_MODEL",
                "base_url": "AUXILIARY_VISION_BASE_URL",
            },
            "approval": {
                "provider": "AUXILIARY_APPROVAL_PROVIDER",
                "model": "AUXILIARY_APPROVAL_MODEL",
                "base_url": "AUXILIARY_APPROVAL_BASE_URL",
            },
        }
        for task_key, env_map in aux_task_env.items():
            task_cfg = auxiliary_cfg.get(task_key, {})
            if not isinstance(task_cfg, dict):
                continue
            prov = str(task_cfg.get("provider", "")).strip()
            model = str(task_cfg.get("model", "")).strip()
            base_url = str(task_cfg.get("base_url", "")).strip()
            if prov and prov != "auto":
                os.environ[env_map["provider"]] = prov
            if model:
                os.environ[env_map["model"]] = model
            if base_url:
                os.environ[env_map["base_url"]] = base_url


# ── Config bridging tests ────────────────────────────────────────────────────


class TestAuxiliaryConfigBridge:
    """Verify the config.yaml → env var bridging logic used by CLI and gateway."""


    def test_vision_model_bridged(self, monkeypatch):
        config = {
            "auxiliary": {
                "vision": {"provider": "auto", "model": "openai/gpt-4o"},
            }
        }
        _run_auxiliary_bridge(config, monkeypatch)
        assert os.environ.get("AUXILIARY_VISION_MODEL") == "openai/gpt-4o"
        # auto provider should not be set
        assert os.environ.get("AUXILIARY_VISION_PROVIDER") is None

    def test_approval_bridged(self, monkeypatch):
        config = {
            "auxiliary": {
                "approval": {"provider": "nous", "model": "gemini-2.5-flash"},
            }
        }
        _run_auxiliary_bridge(config, monkeypatch)
        assert os.environ.get("AUXILIARY_APPROVAL_PROVIDER") == "nous"
        assert os.environ.get("AUXILIARY_APPROVAL_MODEL") == "gemini-2.5-flash"





    def test_mixed_tasks(self, monkeypatch):
        config = {
            "auxiliary": {
                "vision": {"provider": "openrouter", "model": ""},
                "approval": {"provider": "auto", "model": "custom-llm"},
            }
        }
        _run_auxiliary_bridge(config, monkeypatch)
        assert os.environ.get("AUXILIARY_VISION_PROVIDER") == "openrouter"
        assert os.environ.get("AUXILIARY_VISION_MODEL") is None
        assert os.environ.get("AUXILIARY_APPROVAL_PROVIDER") is None
        assert os.environ.get("AUXILIARY_APPROVAL_MODEL") == "custom-llm"





# ── Gateway bridge parity test ───────────────────────────────────────────────


class TestGatewayBridgeCodeParity:
    """Verify the gateway/run.py config bridge contains the auxiliary section."""

    def test_gateway_has_auxiliary_bridge(self):
        """The gateway config bridge must include auxiliary.* bridging.

        After the plugin-aux-task API refactor (2026-05), gateway env-var
        names are derived dynamically (``AUXILIARY_<KEY_UPPER>_*``) so the
        literal strings ``AUXILIARY_VISION_PROVIDER`` etc. no longer appear
        in source. Assert the dynamic shape and the canonical built-in keys
        bridged set instead.
        """
        gateway_path = Path(__file__).parent.parent.parent / "gateway" / "run.py"
        # Pin encoding to UTF-8: source files in this repo are UTF-8, but
        # Path.read_text() defaults to the system locale — which is cp1252
        # on most Western Windows installs and crashes as soon as the file
        # contains any non-ASCII byte (e.g. an em-dash in a comment).
        content = gateway_path.read_text(encoding="utf-8")
        # Dynamic env-var derivation present
        assert 'f"AUXILIARY_{_upper}_PROVIDER"' in content
        # MODEL / BASE_URL are bridged through one field->suffix loop. No API_KEY
        # entry: AUXILIARY_{TASK}_API_KEY has no reader anywhere in the tree.
        assert 'f"AUXILIARY_{_upper}_{_suffix}"' in content
        for field, suffix in (("model", "MODEL"), ("base_url", "BASE_URL")):
            assert f'("{field}", "{suffix}")' in content
        assert '("api_key", "API_KEY")' not in content
        # Built-in bridged keys present
        assert "_aux_bridged_keys" in content
        assert '"vision"' in content
        assert '"approval"' in content
        # web_extract no longer uses an auxiliary LLM (truncate-and-store) —
        # it must NOT be in the bridged set.
        assert '_aux_bridged_keys = {"vision", "approval"}' in content
        # Plugin-aux-task discovery hooked into bridging
        assert "get_plugin_auxiliary_tasks" in content

    def test_gateway_no_compression_env_bridge(self):
        """Gateway should NOT bridge compression config to env vars (config-only)."""
        gateway_path = Path(__file__).parent.parent.parent / "gateway" / "run.py"
        # See note in test_gateway_has_auxiliary_bridge — pin UTF-8 so the
        # test runs on Windows where the default locale is cp1252.
        content = gateway_path.read_text(encoding="utf-8")
        assert "CONTEXT_COMPRESSION_PROVIDER" not in content
        assert "CONTEXT_COMPRESSION_MODEL" not in content


# ── Vision model override tests ──────────────────────────────────────────────


class TestVisionModelOverride:
    """Test that AUXILIARY_VISION_MODEL env var overrides the default model in the handler."""

    @pytest.mark.asyncio
    async def test_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("AUXILIARY_VISION_MODEL", "openai/gpt-4o")
        from tools.vision_tools import _handle_vision_analyze
        with (
            patch("tools.vision_tools.vision_analyze_tool", new_callable=AsyncMock) as mock_tool,
            patch("tools.vision_tools._should_use_native_vision_fast_path", return_value=False),
        ):
            mock_tool.return_value = '{"success": true}'
            await _handle_vision_analyze({"image_url": "http://test.jpg", "question": "test"})
            call_args = mock_tool.call_args
            # 3rd positional arg = model
            assert call_args[0][2] == "openai/gpt-4o"

    @pytest.mark.asyncio
    async def test_default_model_when_no_override(self, monkeypatch):
        monkeypatch.delenv("AUXILIARY_VISION_MODEL", raising=False)
        from tools.vision_tools import _handle_vision_analyze
        with (
            patch("tools.vision_tools.vision_analyze_tool", new_callable=AsyncMock) as mock_tool,
            patch("tools.vision_tools._should_use_native_vision_fast_path", return_value=False),
        ):
            mock_tool.return_value = '{"success": true}'
            await _handle_vision_analyze({"image_url": "http://test.jpg", "question": "test"})
            call_args = mock_tool.call_args
            # With no AUXILIARY_VISION_MODEL env var, model should be None
            # (the centralized call_llm router picks the provider default)
            assert call_args[0][2] is None


# ── DEFAULT_CONFIG shape tests ───────────────────────────────────────────────


class TestDefaultConfigShape:
    """Verify the DEFAULT_CONFIG in hermes_cli/config.py has correct auxiliary structure."""

    def test_auxiliary_section_exists(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert "auxiliary" in DEFAULT_CONFIG

    def test_vision_task_structure(self):
        from hermes_cli.config import DEFAULT_CONFIG
        vision = DEFAULT_CONFIG["auxiliary"]["vision"]
        assert "provider" in vision
        assert "model" in vision
        assert vision["provider"] == "auto"
        assert vision["model"] == ""

    def test_web_extract_task_removed(self):
        """web_extract no longer summarizes via LLM — no aux slot."""
        from hermes_cli.config import DEFAULT_CONFIG
        assert "web_extract" not in DEFAULT_CONFIG["auxiliary"]


# ── CLI defaults parity ─────────────────────────────────────────────────────


class TestCLIDefaultsHaveAuxiliaryKeys:
    """Verify cli.py load_cli_config() defaults dict does NOT include auxiliary
    (it comes from config.yaml deep merge, not hardcoded defaults)."""

    def test_cli_defaults_can_merge_auxiliary(self):
        """The load_cli_config deep merge logic handles keys not in defaults.
        Verify auxiliary would be picked up from config.yaml."""
        # This is a structural assertion: cli.py's second-pass loop
        # carries over keys from file_config that aren't in defaults.
        # So auxiliary config from config.yaml gets merged even though
        # cli.py's defaults dict doesn't define it.
        import cli as _cli_mod
        # See note in test_gateway_has_auxiliary_bridge — pin UTF-8 so the
        # test runs on Windows where the default locale is cp1252.
        source = Path(_cli_mod.__file__).read_text(encoding="utf-8")
        assert "auxiliary_config = defaults.get(\"auxiliary\"" in source
        assert "_AUXILIARY_TASK_ENV" in source
        assert "AUXILIARY_VISION_PROVIDER" in source
        assert "AUXILIARY_VISION_MODEL" in source

    def test_cli_bridge_has_no_api_key_writer(self):
        """cli.py's half of the same removal, asserted the way run.py's half
        is (see test_gateway_has_auxiliary_bridge's api_key check).

        The gateway/run.py bridge's own suite asserted no API-key writer was
        re-added there; nothing in this file executed or inspected cli.py's
        bridge at all, so re-adding the writer here was invisible to the
        whole suite (Wren, PR review). Reads the region by its own
        delimiters and strips comments, so the explanatory NOTE that names
        the variable does not self-trip, and a dynamically assembled
        "AUXILIARY_" + task.upper() + "_API_KEY" is still caught.
        """
        import cli as _cli_mod
        content = Path(_cli_mod.__file__).read_text(encoding="utf-8")
        start = content.index("_AUXILIARY_TASK_ENV = {")
        end = content.index("_CWD_PLACEHOLDERS", start)
        code = "\n".join(
            line for line in content[start:end].splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "api_key" not in code.lower(), (
            "cli.py auxiliary bridge must not read or write api_key"
        )
# appended by wren:i27 round 19 — real-bridge behavioural arms + structural gateway arms
import ast


# ── cli.py's real bridge, driven (not a re-implementation) ───────────────────


def _aux_env_snapshot():
    return {k: v for k, v in os.environ.items() if k.startswith("AUXILIARY_")}


def _drive_cli_bridge(config_dict, monkeypatch):
    """Call the REAL cli.py bridge and return (before, after) full env maps.

    The four bridging cases at the top of this file call a local copy of the
    bridge logic, so nothing in this suite ever executed cli.py's own writer
    (Wren, PR #15 audit, lesson 64).  These arms import cli and call
    ``_export_config_to_env`` directly.
    """
    import cli as _cli_mod
    for k in list(os.environ):
        if k.startswith("AUXILIARY_"):
            monkeypatch.delenv(k, raising=False)
    before = dict(os.environ)
    _cli_mod._export_config_to_env({"auxiliary": config_dict})
    after = dict(os.environ)
    for k in list(after):
        if k.startswith("AUXILIARY_") and k not in before:
            monkeypatch.setenv(k, after[k])
    return before, after


class TestCliBridgeBehaviour:
    """Drive cli.py's own auxiliary bridge, once per claim."""

    def test_real_cli_bridge_writes_provider_model_base_url(self, monkeypatch):
        _b, after = _drive_cli_bridge(
            {"vision": {"provider": "openrouter", "model": "m1",
                        "base_url": "https://example.invalid/v1"}},
            monkeypatch,
        )
        assert after.get("AUXILIARY_VISION_PROVIDER") == "openrouter"
        assert after.get("AUXILIARY_VISION_MODEL") == "m1"
        assert after.get("AUXILIARY_VISION_BASE_URL") == "https://example.invalid/v1"

    def test_real_cli_bridge_each_value_lands_in_its_own_variable(self, monkeypatch):
        """provider/model/base_url must not be cross-wired (the map is data,
        and a swapped pair is invisible to a per-key existence check)."""
        _b, after = _drive_cli_bridge(
            {"approval": {"provider": "P", "model": "M", "base_url": "B"}},
            monkeypatch,
        )
        assert after.get("AUXILIARY_APPROVAL_PROVIDER") == "P"
        assert after.get("AUXILIARY_APPROVAL_MODEL") == "M"
        assert after.get("AUXILIARY_APPROVAL_BASE_URL") == "B"

    def test_real_cli_bridge_skips_auto_provider(self, monkeypatch):
        _b, after = _drive_cli_bridge(
            {"vision": {"provider": "auto", "model": "m2"}}, monkeypatch
        )
        assert "AUXILIARY_VISION_PROVIDER" not in after
        assert after.get("AUXILIARY_VISION_MODEL") == "m2"

    def test_real_cli_bridge_skips_empty_values(self, monkeypatch):
        _b, after = _drive_cli_bridge(
            {"vision": {"provider": "openrouter", "model": "", "base_url": ""}},
            monkeypatch,
        )
        assert after.get("AUXILIARY_VISION_PROVIDER") == "openrouter"
        assert "AUXILIARY_VISION_MODEL" not in after
        assert "AUXILIARY_VISION_BASE_URL" not in after

    def test_real_cli_bridge_writes_no_api_key_anywhere(self, monkeypatch):
        """The whole point of PR #15, asserted over the WHOLE env space.

        Not a watchlist of names (lesson 72): the api_key value must not reach
        os.environ under ANY key, however that key is spelled.
        """
        secret = "sk-audit-canary-2026"
        _b, after = _drive_cli_bridge(
            {"vision": {"provider": "openrouter", "model": "m1",
                        "base_url": "B", "api_key": secret},
             "approval": {"provider": "nous", "model": "m2", "api_key": secret}},
            monkeypatch,
        )
        leaked = {k: v for k, v in after.items() if v == secret}
        assert not leaked, f"api_key reached os.environ as {sorted(leaked)}"
        assert not [k for k in after if k.startswith("AUXILIARY_") and "API_KEY" in k]

    def test_real_cli_bridge_non_vacuity(self, monkeypatch):
        """The negative arm above passes for free if the bridge never runs."""
        _b, after = _drive_cli_bridge(
            {"vision": {"provider": "openrouter", "model": "m1",
                        "base_url": "B", "api_key": "sk-audit-canary-2026"}},
            monkeypatch,
        )
        assert {k for k in after if k.startswith("AUXILIARY_")} >= {
            "AUXILIARY_VISION_PROVIDER",
            "AUXILIARY_VISION_MODEL",
            "AUXILIARY_VISION_BASE_URL",
        }


# ── gateway/run.py: structural, because the bridge sits in start() ───────────


def _key_spellings(node):
    """Every literal spelling an env-var key expression can produce.

    f-strings contribute their literal fragments with ``*`` for each
    interpolation; ``+`` concatenation is flattened; anything else yields the
    empty string (unknown, cannot be judged).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out += part.value
            else:
                out += "*"
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _key_spellings(node.left) + _key_spellings(node.right)
    if isinstance(node, ast.Call):
        return "*"
    if isinstance(node, ast.Name):
        return "*"
    return ""


def _environ_write_keys(tree):
    """Keys of every ``os.environ[<expr>] = ...`` assignment in a tree."""
    keys = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if not isinstance(tgt, ast.Subscript):
                continue
            val = tgt.value
            name = ""
            if isinstance(val, ast.Attribute):
                name = val.attr
            elif isinstance(val, ast.Name):
                name = val.id
            if name != "environ":
                continue
            keys.append(_key_spellings(tgt.slice))
    return keys


def _api_key_writers(source):
    """Env writes whose key can spell AUXILIARY_<anything>_API_KEY."""
    tree = ast.parse(source)
    out = []
    for key in _environ_write_keys(tree):
        norm = key.upper()
        if "API_KEY" in norm.replace("*", "") or (
            norm.startswith("AUXILIARY_") and norm.endswith("_API_KEY")
        ):
            out.append(key)
    return out


_SYNTHETIC_FORBIDDEN = '''
import os
def bridge(upper, cfg):
    os.environ[f"AUXILIARY_{upper}_API_KEY"] = cfg["api_key"]
    os.environ["AUXILIARY_" + upper + "_API_KEY"] = cfg["api_key"]
    os.environ["AUXILIARY_VISION_API_KEY"] = cfg["api_key"]
'''

_SYNTHETIC_CLEAN = '''
import os
def bridge(upper, cfg):
    os.environ[f"AUXILIARY_{upper}_PROVIDER"] = cfg["provider"]
    os.environ["AUXILIARY_" + upper + "_MODEL"] = cfg["model"]
    os.environ["AUXILIARY_VISION_BASE_URL"] = cfg["base_url"]
'''


class TestApiKeyWriterScanner:
    """An arm that asserts a negative must itself be armed (lesson 82)."""

    def test_scanner_catches_all_three_spellings(self):
        found = _api_key_writers(_SYNTHETIC_FORBIDDEN)
        assert len(found) == 3, found

    def test_scanner_clears_the_live_shaped_control(self):
        assert _api_key_writers(_SYNTHETIC_CLEAN) == []


def _repo_root():
    return Path(__file__).parent.parent.parent


class TestNoApiKeyWriterInEitherBridge:
    def test_gateway_run_py_has_no_api_key_writer(self):
        src = (_repo_root() / "gateway" / "run.py").read_text(encoding="utf-8")
        assert _api_key_writers(src) == []

    def test_cli_py_has_no_api_key_writer(self):
        src = (_repo_root() / "cli.py").read_text(encoding="utf-8")
        assert _api_key_writers(src) == []


class TestGatewayAuxiliaryBridgeReachable:
    """A substring arm cannot see ``if False:`` wrapped around the block
    (lesson 79).  start() is not drivable from a test, so the honest arm is
    structural and says so in its name."""

    def _aux_block(self, tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "_aux_bridged_keys":
                        return node
        return None

    def test_bridge_block_exists_and_has_no_constant_false_ancestor(self):
        src = (_repo_root() / "gateway" / "run.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        target = self._aux_block(tree)
        assert target is not None, "_aux_bridged_keys assignment is gone"
        # Build parent links, then walk the chain to module scope.
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        cur, chain = target, []
        while cur in parents:
            cur = parents[cur]
            chain.append(cur)
        dead = []
        for node in chain:
            if isinstance(node, (ast.If, ast.While)):
                t = node.test
                if isinstance(t, ast.Constant) and not t.value:
                    dead.append(node.lineno)
        assert not dead, f"auxiliary bridge is unreachable, gated at line(s) {dead}"

    def test_bridge_writes_the_three_live_variables(self):
        src = (_repo_root() / "gateway" / "run.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        keys = set(_environ_write_keys(tree))
        assert "AUXILIARY_*_PROVIDER" in keys, sorted(k for k in keys if k.startswith("AUXILIARY"))
        # MODEL / BASE_URL are written through one f"AUXILIARY_{_upper}_{_suffix}" loop over
        # (field, suffix) pairs; read the suffixes out of that loop's literal table.
        assert "AUXILIARY_*_*" in keys, sorted(k for k in keys if k.startswith("AUXILIARY"))
        suffixes = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple):
                for elt in node.iter.elts:
                    if (isinstance(elt, ast.Tuple) and len(elt.elts) == 2
                            and all(isinstance(e, ast.Constant) for e in elt.elts)):
                        suffixes.add(elt.elts[1].value)
        for suffix in ("MODEL", "BASE_URL"):
            assert suffix in suffixes, (suffix, sorted(suffixes))
        assert "API_KEY" not in suffixes, sorted(suffixes)


# Case-count floor: this suite is run by pytest, which reports its own count,
# but a collection error can still leave a truthy-looking partial run.
# Floor set from the MEASURED count after the round-19 run.
def test_case_count_floor():
    import inspect as _inspect
    mod = sys.modules[__name__]
    n = 0
    for _name, obj in vars(mod).items():
        if _name.startswith("test_") and callable(obj):
            n += 1
        elif _inspect.isclass(obj) and _name.startswith("Test"):
            n += sum(1 for m in vars(obj) if m.startswith("test_"))
    assert n >= 25, f"suite shrank to {n} cases"
