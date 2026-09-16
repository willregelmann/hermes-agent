"""Provider module registry.

Provider profiles can live in three places:

1. Bundled plugins: ``plugins/model-providers/<name>/`` (shipped with hermes-agent)
2. User plugins: ``$HERMES_HOME/plugins/model-providers/<name>/``
3. Pip-installed plugins: distributions exposing a ``hermes_agent.plugins``
   entry point (``module:func`` callable or a self-registering ``module``)

Each plugin directory contains:
  - ``__init__.py`` — calls ``register_provider(profile)`` at import
  - ``plugin.yaml`` — manifest (name, kind: model-provider, version, description)

Discovery is lazy: the first call to ``get_provider_profile()`` or
``list_providers()`` scans both locations and imports every plugin. User
plugins override bundled plugins on name collision (last-writer-wins), so
third parties can monkey-patch or replace any built-in profile without
editing the repo.

For backward compatibility, ``providers/*.py`` files (other than ``base.py``
and ``__init__.py``) are still discovered via ``pkgutil.iter_modules``.
This lets out-of-tree users drop a single-file profile into an editable
install without the plugin dir structure. New profiles should prefer the
plugin layout.

Usage::

    from providers import get_provider_profile
    profile = get_provider_profile("nvidia")   # ProviderProfile or None
    profile = get_provider_profile("kimi")     # checks name + aliases
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from contextvars import ContextVar
from pathlib import Path

from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scoping (wren:i24, final member).
#
# ``_REGISTRY``/``_ALIASES`` hold BUNDLED + pip-entry-point + legacy
# ``providers/<name>.py`` profiles. Those are process-global by construction:
# the bundled directory and the installed package are the same for every
# profile in a multiplex gateway, so sharing them is correct, not a bug.
#
# ``$HERMES_HOME/plugins/model-providers/`` and the flat
# ``$HERMES_HOME/plugins/<name>/`` (kind: model-provider) directories are
# NOT process-global — they are per-profile by definition. Before this fix,
# ``register_provider()`` wrote every registration into the same global
# ``_REGISTRY`` regardless of source, so the first profile discovered in a
# process silently pinned every other profile's user-plugin providers
# (including overrides of a bundled name) until restart. A second bug rode
# alongside it: ``_import_plugin_dir``'s ``sys.modules`` skip-guard for user
# plugins was keyed by plugin dirname only, so a second profile with a
# same-named but DIFFERENT ``$HERMES_HOME/plugins/model-providers/<name>/``
# directory was never imported at all — its registration silently vanished.
#
# Fix: user/installed plugin imports run with ``_CURRENT_SCOPE`` set to the
# resolved-home key for that discovery pass (see ``_discover_providers``).
# ``register_provider()`` reads it and files the registration into a
# per-home layer when set, global otherwise. Lookups merge the current
# home's layer over the global one (home wins on name collision — same
# override semantics as before, just correctly isolated). The user-plugin
# import guard is keyed by (home, dirname) so two profiles' distinct plugin
# trees are never conflated.
_CURRENT_SCOPE: ContextVar[str | None] = ContextVar("_CURRENT_SCOPE", default=None)

_REGISTRY: dict[str, ProviderProfile] = {}
_ALIASES: dict[str, str] = {}
_SCOPED_REGISTRY: dict[str, dict[str, ProviderProfile]] = {}
_SCOPED_ALIASES: dict[str, dict[str, str]] = {}
_PROVIDER_LIST_CACHE: dict[str, list[ProviderProfile]] = {}
_discovered = False
_discovered_homes: set[str] = set()

# Repo-root ``plugins/model-providers/`` — populated at discovery time.
_BUNDLED_PLUGINS_DIR = (
    Path(__file__).resolve().parent.parent / "plugins" / "model-providers"
)


def _scope_key() -> str:
    """Return the resolved-home key for the calling profile's discovery pass."""
    from hermes_constants import hermes_home_key

    return hermes_home_key()


def register_provider(profile: ProviderProfile) -> None:
    """Register a provider profile by name and aliases.

    Later registrations with the same name replace earlier ones — so user
    plugins under ``$HERMES_HOME/plugins/model-providers/`` can override
    bundled profiles without editing repo code.

    Filed into the per-home layer (see module docstring) when called during
    a user/installed-plugin import; into the global layer otherwise (bundled,
    pip entry point, legacy ``providers/<name>.py``).
    """
    global _PROVIDER_LIST_CACHE
    scope = _CURRENT_SCOPE.get()
    registry = _REGISTRY if scope is None else _SCOPED_REGISTRY.setdefault(scope, {})
    aliases = _ALIASES if scope is None else _SCOPED_ALIASES.setdefault(scope, {})
    registry[profile.name] = profile
    for alias in profile.aliases:
        aliases[alias] = profile.name
    _PROVIDER_LIST_CACHE = {}


def get_provider_profile(name: str) -> ProviderProfile | None:
    """Look up a provider profile by name or alias.

    Returns None if the provider has no profile (falls back to generic).
    Checks the calling profile's home-scoped overrides before the global
    (bundled/pip/legacy) registry.
    """
    _ensure_discovered()
    home = _scope_key()
    scoped_aliases = _SCOPED_ALIASES.get(home, {})
    scoped_registry = _SCOPED_REGISTRY.get(home, {})
    canonical = scoped_aliases.get(name) or _ALIASES.get(name, name)
    profile = scoped_registry.get(canonical) or _REGISTRY.get(canonical)
    # Named custom routes share the generic wire policy unless a plugin
    # explicitly registered that route. Other names retain exact lookup.
    if profile is None and isinstance(name, str) and name.lower().startswith("custom:"):
        profile = scoped_registry.get("custom") or _REGISTRY.get("custom")
    return profile


def routed_model_rejects_vision_tool_messages(provider: str, model: str) -> bool:
    """Whether an active route or its aggregator-targeted model rejects image tool parts.

    Routing aggregators such as ``openrouter`` send vendor-prefixed model IDs
    (for example, ``xiaomi/mimo-v2.5``), but their own profile cannot describe
    every routed provider's tool-message compatibility. Preserve the transport
    profile as the default and consult a registered target profile only for
    routing aggregators. Missing or unrecognized identities deliberately fail open.
    """
    provider_name = str(provider or "").strip().lower()
    profile = get_provider_profile(provider_name)
    if profile is not None and profile.supports_vision_tool_messages is False:
        return True
    # Routing aggregators accept a ``vendor/model`` identifier while the request is sent
    # to the aggregator; the target provider can have stricter message-shape support than
    # the aggregator's generic OpenAI-compatible transport profile.
    from hermes_cli.providers import is_routing_aggregator
    if not is_routing_aggregator(provider_name):
        return False

    target_name, separator, _ = str(model or "").strip().partition("/")
    if not separator or not target_name:
        return False
    target_profile = get_provider_profile(target_name.strip().lower())
    return target_profile is not None and target_profile.supports_vision_tool_messages is False


def list_providers() -> list[ProviderProfile]:
    """Return all registered provider profiles (one per canonical name),
    the calling profile's home-scoped plugins overlaid on the global set."""
    _ensure_discovered()
    home = _scope_key()
    cached = _PROVIDER_LIST_CACHE.get(home)
    if cached is not None:
        return list(cached)
    # Home-scoped entries win on name collision (mirrors get_provider_profile).
    merged: dict[str, ProviderProfile] = dict(_REGISTRY)
    merged.update(_SCOPED_REGISTRY.get(home, {}))
    # Deduplicate: canonical-name dict may still alias the same object twice
    # via _ALIASES-derived entries; keep first-seen by identity.
    seen: set[int] = set()
    result: list[ProviderProfile] = []
    for profile in merged.values():
        pid = id(profile)
        if pid not in seen:
            seen.add(pid)
            result.append(profile)
    _PROVIDER_LIST_CACHE[home] = result
    return list(result)


def _user_plugins_dir() -> Path | None:
    """Return ``$HERMES_HOME/plugins/model-providers/`` if it exists."""
    try:
        from hermes_constants import get_hermes_home

        d = get_hermes_home() / "plugins" / "model-providers"
        return d if d.is_dir() else None
    except Exception:
        return None


def _installed_plugins_dir() -> Path | None:
    """Return ``$HERMES_HOME/plugins/`` if it exists.

    This is where ``hermes plugins install`` clones a plugin — flat, one
    directory per plugin, NOT under ``model-providers/``. See
    :func:`_discover_installed_provider_plugins`.
    """
    try:
        from hermes_constants import get_hermes_home

        d = get_hermes_home() / "plugins"
        return d if d.is_dir() else None
    except Exception:
        return None


def _declares_model_provider_kind(plugin_dir: Path) -> bool:
    """Whether ``plugin_dir``'s manifest declares ``kind: model-provider``.

    Only that kind is imported from the flat install directory — every other
    plugin there belongs to ``PluginManager``, which owns its lifecycle and
    consent flow. Parsed with PyYAML when available, falling back to a line
    scan so provider discovery never hard-depends on it.
    """
    for filename in ("plugin.yaml", "plugin.yml"):
        manifest = plugin_dir / filename
        if not manifest.is_file():
            continue
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return False
        try:
            import yaml

            data = yaml.safe_load(text)
            if isinstance(data, dict):
                return str(data.get("kind", "")).strip() == "model-provider"
        except Exception:
            pass
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            if key.strip() == "kind":
                return value.strip().strip("\"'") == "model-provider"
        return False
    return False


def _import_plugin_dir(plugin_dir: Path, source: str, *, home: str | None = None) -> None:
    """Import a single plugin directory so it self-registers.

    ``source`` is "bundled" or "user", used only for log messages. ``home``
    is the resolved ``HERMES_HOME`` key this plugin directory belongs to
    (only meaningful for ``source="user"``; bundled plugins are shared).

    The ``sys.modules`` skip-guard was previously keyed by dirname alone
    (``_hermes_user_provider_<name>``), so two profiles with DIFFERENT
    directories of the same plugin name (e.g. two ``$HERMES_HOME``s each
    with their own ``model-providers/acme/``) collided: the second profile's
    import was silently skipped as "already imported" and its provider was
    never registered under that profile at all. User-plugin module names now
    include the home key so distinct directories never alias.
    """
    init_file = plugin_dir / "__init__.py"
    if not init_file.exists():
        return

    # Give bundled plugins a stable import path (``plugins.model_providers.<name>``)
    # so relative imports within the plugin work — one copy, shared globally.
    # User plugins load via ``importlib.util.spec_from_file_location`` with a
    # module name unique per (home, dirname) so distinct HERMES_HOME plugin
    # trees of the same plugin name never alias each other's module object.
    safe_name = plugin_dir.name.replace("-", "_")
    if source == "bundled":
        module_name = f"plugins.model_providers.{safe_name}"
    else:
        home_tag = (home or "").replace("/", "_").replace("\\", "_").replace(":", "_")
        module_name = f"_hermes_user_provider_{home_tag}_{safe_name}"

    if module_name in sys.modules:
        return  # already imported

    scope_token = None
    if source != "bundled":
        scope_token = _CURRENT_SCOPE.set(home or _scope_key())
    try:
        spec = importlib.util.spec_from_file_location(
            module_name, init_file, submodule_search_locations=[str(plugin_dir)]
        )
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        logger.warning(
            "Failed to load %s provider plugin %s: %s", source, plugin_dir.name, exc
        )
        sys.modules.pop(module_name, None)
    finally:
        if scope_token is not None:
            _CURRENT_SCOPE.reset(scope_token)


def _discover_entry_point_providers() -> None:
    """Import pip-installed provider plugins via the ``hermes_agent.plugins``
    entry-point group so they self-register.

    A distribution ships::

        [project.entry-points."hermes_agent.plugins"]
        acme-inference = "acme_hermes_plugin:register"

    The target may be either a **callable** (``module:func`` — invoked with no
    args; typically calls ``register_provider(profile)``) or a **module**
    (``module`` — imported for its module-level ``register_provider`` side
    effect, mirroring the directory-plugin ``__init__.py`` contract).

    Gating and safety:

    * **Opt-in.** Entry-point plugins are subject to the same
      ``plugins.enabled`` allow-list (and ``plugins.disabled`` deny-list) the
      general PluginManager enforces — a pip package is never imported just
      because it is installed. An entry point whose name is not enabled is
      skipped without loading.
    * **Provider targets only.** The ``hermes_agent.plugins`` group is shared
      with general plugins whose target is ``register(ctx)``. Callables that
      require arguments are skipped here (the PluginManager owns them);
      provider registration hooks take no arguments by contract.

    Failures are swallowed per-entry (a broken third-party package must not
    break provider discovery) and logged at warning level. This scan runs
    first, so filesystem plugins (bundled + ``$HERMES_HOME``) keep their
    documented override precedence via last-writer-wins in
    ``register_provider()`` — a pip package cannot hijack a first-party
    provider name.
    """
    try:
        import importlib.metadata as _md
    except Exception:  # pragma: no cover — importlib.metadata always present ≥3.8
        return

    # Same opt-in gate as the general PluginManager: only entry points named
    # in ``plugins.enabled`` load, and ``plugins.disabled`` always wins.
    try:
        from hermes_cli.plugins import _get_disabled_plugins, _get_enabled_plugins

        enabled = _get_enabled_plugins()  # None = nothing enabled yet (opt-in default)
        disabled = _get_disabled_plugins()
    except Exception:  # pragma: no cover — config layer unavailable
        enabled, disabled = None, set()
    if not enabled:
        return

    group = "hermes_agent.plugins"
    try:
        eps = _md.entry_points()
        # Python 3.10+ exposes .select(); older returns a dict-like mapping.
        if hasattr(eps, "select"):
            group_eps = list(eps.select(group=group))
        else:  # pragma: no cover — legacy interpreters
            group_eps = list(eps.get(group, []))  # type: ignore[attr-defined]
    except Exception as exc:
        logger.debug("entry-point provider scan skipped: %s", exc)
        return

    for ep in group_eps:
        if ep.name not in enabled or ep.name in disabled:
            logger.debug(
                "entry-point provider %r skipped: not enabled in config", ep.name
            )
            continue
        try:
            loaded = ep.load()
        except Exception as exc:
            logger.warning(
                "Failed to load entry-point provider plugin %r: %s", ep.name, exc
            )
            continue
        # ``module:func`` → callable we invoke; bare ``module`` → import side
        # effect already happened during load(). Only call when it's callable
        # AND zero-arg: general plugins in this shared group expose
        # ``register(ctx)`` (requires an argument) and belong to the
        # PluginManager, not the provider registry.
        if callable(loaded):
            if _requires_arguments(loaded):
                logger.debug(
                    "entry-point %r skipped by provider scan: target requires "
                    "arguments (general plugin owned by PluginManager)",
                    ep.name,
                )
                continue
            try:
                loaded()
            except Exception as exc:
                logger.warning(
                    "Entry-point provider plugin %r raised on invocation: %s",
                    ep.name,
                    exc,
                )


def _requires_arguments(fn) -> bool:
    """True when ``fn`` cannot be called with zero arguments.

    Used to distinguish provider registration hooks (zero-arg by contract)
    from general plugin hooks (``register(ctx)``) sharing the same entry-point
    group. Unintrospectable callables (C extensions) are treated as zero-arg
    and left to the per-entry exception guard.
    """
    import inspect

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover — builtins/C callables
        return False
    for param in sig.parameters.values():
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ) and param.default is inspect.Parameter.empty:
            return True
    return False


def _ensure_discovered() -> None:
    """Run global discovery once, then this profile's home-scoped discovery
    once. Safe to call on every lookup — both steps are idempotent guards."""
    if not _discovered:
        _discover_providers()
    home = _scope_key()
    if home not in _discovered_homes:
        _discover_home_providers(home)


def _discover_providers() -> None:
    """Populate the GLOBAL registry: sources shared by every profile.

    Order:
      0. Pip-installed plugins (entry points)
      1. Bundled plugins at ``<repo>/plugins/model-providers/<name>/``
      3. Legacy per-file modules at ``providers/<name>.py`` (back-compat)

    These are process-wide by construction — the installed package and the
    bundled directory are identical for every ``HERMES_HOME`` in the process,
    so one discovery pass correctly serves all of them. Per-``HERMES_HOME``
    sources (steps "2"/"2b" in the old single-pass design) are handled
    separately by :func:`_discover_home_providers`, once per home seen.

    Each step imports its plugins, which call ``register_provider()`` at
    module-level. Later steps win on name collision.
    """
    global _discovered
    if _discovered:
        return
    _discovered = True

    # 0. Pip-installed plugins — entry points in the ``hermes_agent.plugins``
    #    group (the same group the general PluginManager uses). The manager
    #    records model-provider manifests for introspection but deliberately
    #    does NOT import them — provider lifecycle is owned here — so without
    #    this step a ``pip install``ed provider never calls
    #    ``register_provider()`` and is never selectable.
    #
    #    Discovered FIRST, i.e. lowest precedence: because
    #    ``register_provider()`` is last-writer-wins, running this before the
    #    filesystem steps means a bundled or ``$HERMES_HOME`` profile of the
    #    same name always overrides a pip-installed one. That prevents a
    #    third-party package from silently hijacking a first-party provider
    #    name (e.g. ``openrouter``) while still letting pip packages add
    #    genuinely new providers.
    _discover_entry_point_providers()

    # 1. Bundled plugins — shipped with hermes-agent.
    if _BUNDLED_PLUGINS_DIR.is_dir():
        for child in sorted(_BUNDLED_PLUGINS_DIR.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            _import_plugin_dir(child, "bundled")

    # 3. Legacy single-file profiles at providers/<name>.py. Kept for
    #    back-compat — if someone drops a ``providers/foo.py`` into an
    #    editable install, it still works without the plugin layout.
    try:
        import pkgutil

        import providers as _pkg

        for _importer, modname, _ispkg in pkgutil.iter_modules(_pkg.__path__):
            if modname.startswith("_") or modname == "base":
                continue
            try:
                importlib.import_module(f"providers.{modname}")
            except ImportError as exc:
                logger.warning(
                    "Failed to import legacy provider module %s: %s", modname, exc
                )
    except Exception:
        pass

    # (Pip entry-point providers are discovered in step 0, before the
    # filesystem plugins, so first-party profiles always win on name
    # collision — see _discover_entry_point_providers.)


def _discover_home_providers(home: str) -> None:
    """Populate ``home``'s scoped registry from its ``$HERMES_HOME`` plugin dirs.

    Order (mirrors the old steps 2/2b of the single-pass design):
      2. User plugins at ``$HERMES_HOME/plugins/model-providers/<name>/``
      2b. Plugins installed by ``hermes plugins install`` at
          ``$HERMES_HOME/plugins/<name>/`` that declare ``kind: model-provider``

    Runs once per resolved home (tracked in ``_discovered_homes``), the same
    guarantee ``_discover_providers`` gives the global sources. ``home`` is
    threaded through explicitly (rather than re-resolved per plugin dir) so
    every registration and the ``sys.modules`` import guard agree on which
    profile they belong to even if ``$HERMES_HOME``/context changes between
    calls on the same thread.
    """
    _discovered_homes.add(home)

    # 2. User plugins — under $HERMES_HOME/plugins/model-providers/<name>/.
    #    These can override any bundled profile of the same name (last-writer-wins
    #    in register_provider(), scoped to this home).
    user_dir = _user_plugins_dir()
    if user_dir is not None:
        for child in sorted(user_dir.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            _import_plugin_dir(child, "user", home=home)

    # 2b. Plugins installed by ``hermes plugins install`` / the plugin index.
    #     Those clone into $HERMES_HOME/plugins/<name>/ — flat, NOT under
    #     model-providers/ — so step 2 never sees them. PluginManager does not
    #     import them either: it classifies ``kind: model-provider`` and routes
    #     it here on purpose. Without this step the documented install path
    #     silently half-works — the CLI reports success and the provider does
    #     not exist. Only manifests declaring that kind are imported; every
    #     other plugin in this directory belongs to PluginManager.
    installed_dir = _installed_plugins_dir()
    if installed_dir is not None:
        for child in sorted(installed_dir.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            if child.name == "model-providers":
                continue  # handled by step 2
            if not _declares_model_provider_kind(child):
                continue
            _import_plugin_dir(child, "user", home=home)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.


_PLUGIN_COMPAT_LAZY = {
    'OMIT_TEMPERATURE': ('providers.base', 'OMIT_TEMPERATURE'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
