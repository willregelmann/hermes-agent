# Profile

> **A profile is one independent Hermes agent.** It is a Hermes home directory plus the
> secret scope and terminal scope derived from it. Everything Hermes persists about an agent
> (its config, credentials, persona, memory, skills, sessions, cron jobs and gateway state)
> lives in that one directory.

Profiles are the unit of isolation in Hermes. The rule is that one profile's work never
observes another profile's state. They are independent islands by design: nothing is
inherited live between profiles, and a clone is a one-time copy.

## Identity

| Aspect | Value |
|---|---|
| Canonical id | `default`, or a name matching `^[a-z0-9][a-z0-9_-]{0,63}$` that is not reserved (`hermes`, `default`, `test`, `tmp`, `root`, `sudo`) |
| Normalization | Input is lowercased and `Default` becomes `default` (`normalize_profile_name`). Validate only after normalizing. |
| Home directory | `default` is the Hermes root itself: `~/.hermes`, `%LOCALAPPDATA%\hermes` on Windows, or `HERMES_HOME` in Docker and custom roots such as `/opt/data`. A named profile lives at `<root>/profiles/<id>/`. |
| Home to id | `hermes_constants.profile_name_for_home(path)` |
| Registry key | `hermes_constants.hermes_home_key(path)` is the stable key for process-global per-profile slots, such as MCP discovery and tool-registry overlays. |
| Display name | `display_name` in `<home>/profile.yaml` is for presentation only. Resolution, comparison, `-p`, service names and cron always use the canonical id. |
| Gateway namespace | Session keys start with `agent:<id>:`. The default profile keeps `agent:main:`, and a profile literally named `main` is keyed `agent:main~:`. |

The root is not the same as the current home. Profile *management* (list, create, delete) is
anchored at the root: `_get_profiles_root()` is `<root>/profiles`, so `hermes -p coder profile
list` still sees every profile. This is intentional.

## What a profile owns

A new profile is bootstrapped with these directories: `memories/`, `sessions/`, `skills/`,
`skins/`, `logs/`, `plans/`, `workspace/`, `cron/` and `home/` (`_PROFILE_DIRS`). Its files include:

| Path | What it holds |
|---|---|
| `config.yaml` | All behavioural settings: model, toolsets, `terminal.*`, gateway, MCP servers |
| `.env` | Secrets only: provider keys and bot tokens. This is the source of the profile's **secret scope**. |
| `SOUL.md` | Persona and standing instructions |
| `memories/MEMORY.md`, `memories/USER.md` | Curated memory, injected into the system prompt when a session starts |
| `state.db` | Every [session](session.md) of this profile, plus usage, gateway routing and the delivery ledger |
| `skills/`, `plugins/` | Installed skills and profile-private plugins |
| `cron/` | Scheduled jobs, which are bound to this profile and its delivery channels |
| `logs/` | `agent.log`, `errors.log`, `gateway.log` |
| `profile.yaml` | Metadata *about* the profile: `description` (used for kanban routing) and `display_name` |
| `gateway.pid`, `gateway_state.json`, `processes.json` | Runtime state of this profile's gateway and background processes |

The profile's `terminal.*` settings, together with the `TERMINAL_*` values in its `.env`, form its **terminal scope**.
That scope is the complete execution policy: backend, cwd, SSH target and `home_mode`.

## What is shared, not owned

These are shared between profiles on purpose:

- **The code install.** `hermes update` pulls once and syncs bundled skills into every profile.
  It never overwrites skills the user has modified.
- **The profile registry.** This is `<root>/profiles/`, the sticky `<root>/active_profile` file,
  tombstones in `profiles/.deleted/`, and the command aliases in `~/.local/bin/`.
- **OAuth logins with single-use refresh tokens** (Anthropic, OpenAI Codex, xAI). Every profile
  reads them from the root `auth.json`, and a refresh from any profile is written back to root.
  A copy would be the same credential with two owners, so clones drop these rows. Static API keys
  are copied as normal.
- **The OS user's `HOME` for tool subprocesses** (on host installs by default). This lets `git`,
  `ssh`, `gh` and similar tools find their credentials. Set `terminal.home_mode: profile` to use
  `<home>/home` instead.
- **The filesystem.** A profile is not a sandbox.
- **Under a multiplexed gateway:** the process, its PID and lock, `gateway_state.json` (in the
  default home), the single HTTP listener, and the `profile_routes` table (declared on the default
  profile).

## How the active profile is chosen

**Single-profile processes** are the CLI, `hermes -p x <cmd>` and a standalone gateway.
`_apply_profile_override()` in `hermes_cli/main.py` runs before any other Hermes module is imported.
It picks the first match from this list and writes it to `os.environ["HERMES_HOME"]`:

1. An explicit `-p/--profile` flag. A profile alias such as `coder` is a wrapper script for `hermes -p coder`.
2. A `HERMES_HOME` that already points at `<root>/profiles/<id>`.
3. `<root>/active_profile`, which `hermes profile use <id>` sets.
4. `default`.

**Multi-profile processes** are the multiplexed gateway (`gateway.multiplex_profiles`), the
Desktop/dashboard `serve` backend and the cron ticker. In these, `os.environ` keeps the *launch*
profile's values. Each piece of work binds the active profile itself, using context variables:

```
get_hermes_home():  context override  →  HERMES_HOME env var  →  platform default
get_secret(name):   global allowlist  →  active secret scope   →  os.environ (only when not multiplexing)
terminal_env(key):  active terminal scope (complete policy; omitted keys → defaults, never ambient env)
```

## Invariants

1. **Profiles are islands.** There is no live inheritance of config, memory or skills between
   profiles. `--clone` copies once when the profile is created, and the copy diverges from then on.
   A pull request that coupled profiles would fight the design; see the root `AGENTS.md` under
   "Intentional design, not a gap".
2. **One agent per profile.** Don't point two differently-purposed agents at one home. Each would
   load the other's memory writes into its system prompt. Agents that need shared memory should use
   an external memory provider. A CLI and a gateway of the *same* agent can share a home. They
   coordinate through `state.db` (WAL mode plus a per-session turn lease).
3. **Resolve at call time; never capture.** Use `get_hermes_home()` for paths and
   `display_hermes_home()` only for text shown to users. Never hardcode `~/.hermes`. A module
   constant derived from the home, the config or `.env` freezes to the launch profile. That is a
   bug class: key the slot by `hermes_home_key()` or resolve the value per call.
4. **Bind all three scopes together.** Home, secret and terminal scope form one binding. Binding
   the home alone still leaves credentials coming from the launch profile's `.env`. Binding home
   and secrets without the terminal scope lets a `docker` profile run on the launch process's
   `local` backend. The canonical binders are `gateway/run.py::_profile_runtime_scope` for a turn
   and `tui_gateway/model_switch.py::_session_profile_runtime_scope` for RPC and teardown.
5. **Bind the owner for work outside a turn, too.** Eviction, shutdown, memory flush,
   `on_session_end`, deferred callbacks, notifiers, tickers, boot probes (`check_fn`, MCP discovery,
   hooks) and thread hops all run for a specific profile. Resolve the owning home from the record
   you are acting on, such as the session's `profile_home`, its `agent:<id>:` key, or
   `Path(session_db.db_path).parent`. Never resolve it from `os.environ`.
6. **Fail closed when multiplexing.** Once `set_multiplex_active(True)` has been called,
   `get_secret()` with no scope installed raises `UnscopedSecretError`. A scoped miss returns the
   default, never `os.environ`. An unbound read in a single-profile process silently falls back to
   the default profile rather than raising an error, which is exactly why rule 5 exists.
7. **Children receive the profile explicitly.** Spawn with
   `tools/environments/local.py::served_profile_child_env`, never with `os.environ.copy()`. Service
   units (systemd, launchd, Scheduled Task) carry `HERMES_HOME` explicitly because a supervisor
   starts with an empty environment.
8. **A messaging channel belongs to exactly one profile.** Adapters take a token lock
   (`gateway.status.acquire_scoped_lock`), so two profiles cannot share a bot credential. Clones
   strip messaging channels unless the user passes `--clone-channels`.
9. **Never create a profile implicitly.** Enumerating profiles only reads directories.
   `mkdir_under_hermes_home()` refuses to create a missing or tombstoned named profile, so a late
   write cannot bring a deleted profile back.

## Lifecycle

| Stage | Command | What happens |
|---|---|---|
| Create | `hermes profile create <id>` | Bootstraps the layout, seeds bundled skills (unless `--no-skills`), writes a placeholder `.env` and creates the `~/.local/bin/<id>` alias |
| ↳ clone | `--clone` / `--clone-from <src>` | Copies `config.yaml`, `.env`, `SOUL.md`, `MEMORY.md` and `USER.md`, but not messaging channels |
| ↳ full clone | `--clone-all` | Copies everything except history (`state.db`, `sessions/`, `backups/`, `state-snapshots/`, `checkpoints/`), `cron/` and runtime files |
| ↳ install | `hermes profile install <git-url>` | Builds the profile from a versioned distribution (see `profile-distributions.md`) |
| ↳ import | `hermes profile import <archive>` | Restores an exported archive as a new profile |
| Select | `hermes profile use <id>` / `-p <id>` | Sets the sticky default, or selects a profile for one command |
| Rename | `hermes profile rename <old> <new>` | Moves the directory, the alias and the service. For `default`, it only sets `display_name`. |
| Export | `hermes profile export <id>` | Writes a portable `.tar.gz` with credentials stripped |
| Delete | `hermes profile delete <id>` | Stops the gateway, removes the service and the alias, writes a tombstone and deletes the data. `default` cannot be deleted. |

Clones are built in a hidden `profiles/.<id>.staging-<pid>` directory and published with a single
`os.rename`. A running multiplexer therefore never sees a half-copied profile.

## Relationships

- **Profile → [Session](session.md) (one to many).** Every session belongs to exactly one profile
  and is stored in that profile's `state.db`. Its `sessions.profile_name` column names the owner.
- **Profile → Gateway.** A profile is served either by its own gateway process or by one
  multiplexed gateway. The multiplexed gateway serves `default` plus every live named profile
  (`profiles_to_serve`); there is no allowlist.
- **Profile → Cron, skills, MCP servers, plugins, memory providers.** All of these are per profile.
  The process-global slots that hold them are keyed by `hermes_home_key()`.

## Not to be confused with

- **Workspace / working directory.** This is where terminal commands start, set by
  `terminal.cwd`. It is separate from the profile directory. `cwd: "."` means the directory Hermes
  was launched from.
- **Sandbox.** Profiles do not restrict filesystem access. A sandbox comes from the terminal
  backend (docker, ssh, …).
- **`HOME`.** `HERMES_HOME` is the profile boundary. `HOME` is the OS user's home, which external
  CLIs expect.
- **Toolset / skin / personality.** These are settings *inside* a profile, not alternatives to one.

## Code map

| Concern | Where |
|---|---|
| Home resolution, overrides, tombstones | `hermes_constants.py` (`get_hermes_home`, `set_hermes_home_override`, `hermes_home_key`, `profile_name_for_home`, `mkdir_under_hermes_home`) |
| Names, CRUD, clone, serve set | `hermes_cli/profiles.py` |
| CLI surface | `hermes_cli/profile_cmd.py` |
| Channel stripping on clone | `hermes_cli/profile_channels.py` |
| Distributions | `hermes_cli/profile_distribution.py` |
| Selection at startup | `hermes_cli/main.py::_apply_profile_override` |
| Secret scope | `agent/secret_scope.py` |
| Terminal scope | `tools/terminal_scope.py` |
| Scope binders | `gateway/run.py::_profile_runtime_scope`, `tui_gateway/server.py::@_profile_scoped`, `tui_gateway/model_switch.py::_session_profile_runtime_scope`, `cron/scheduler_provider.py::_profile_cron_scope`, `gateway/run_agent_cache.py::_run_release_in_profile_scope` |
| Advisory lint | `scripts/check_profile_scope_patterns.py` |

## Further reading

- [Profiles (user guide)](../../website/docs/user-guide/profiles.md)
- [What is isolated per profile](../../website/docs/user-guide/multi-profile-gateways.md#what-is-isolated-per-profile)
- [Multiplexing gateway internals](../../website/docs/developer-guide/multiplexing-gateway.md)
- [Profile distributions](../../website/docs/user-guide/profile-distributions.md)
- `gateway/AGENTS.md` § Profile scope and `hermes_cli/AGENTS.md` § Profiles
