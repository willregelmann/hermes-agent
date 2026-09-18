# Session

> **A session is one continuous conversation between a [profile](profile.md)'s agent and its
> user or users.** It is an ordered transcript of messages under one session id, stored in the
> owning profile's `state.db`. Its system prompt stays byte-stable for the session's whole life.

Most costs and guarantees in Hermes are defined per session: the prompt cache, strict role
alternation, memory injection, token and cost accounting, and history search. The durable
transcript in `state.db` is the source of truth. An in-memory agent is only a warm runtime
attached to it.

## Identity

Three different identifiers are called "session" something. Keep them apart:

| Identifier | Shape | What it names | Where it lives |
|---|---|---|---|
| **Session id** | Usually `YYYYMMDD_HHMMSS_<hex>` (`hermes_state_ids.new_session_id`). Some surfaces mint prefixed ids, such as cron runs (`cron_<job_id>_<timestamp>`) and API-server rooms (`room_<hash>`). Treat the id as opaque. | One conversation. This is the durable primary key. | `sessions.id` in the profile's `state.db` |
| **Gateway session key** | `agent:<profile-ns>:<platform>:<chat_type>[:<chat_id>][:<thread_id>][:<participant>]` | A *lane*: one chat, thread or user on one platform. A lane points at one session id at a time, and `/new` re-points it. | `gateway_routing` table. `sessions/sessions.json` is a legacy mirror of it. |
| **TUI/Desktop runtime sid** | 8 hex characters | One live attachment of a client to a session in `tui_gateway`. In that session dict, the field named `session_key` holds the DB **session id**, not a gateway key. | Process memory only |

A session id is unique within one `state.db`, which means within one profile. Across profiles,
address a session as `@session:<profile>/<id>`. A **title** is optional. It is unique among
non-NULL titles in the store, and a lineage continues as `name #2`, `name #3`.

## What a session owns

- **A `sessions` row.** It holds `source` (`cli`, `telegram`, `cron`, `subagent`, …), model and
  `model_config`, the system prompt (deduplicated through `system_prompt_hash` into the
  `system_prompts` table), `parent_session_id`, `started_at`/`ended_at`/`end_reason`, token and cost
  totals, title, workspace (`cwd`, `git_repo_root`, `git_branch`), `profile_name`, and the gateway
  origin (`session_key`, `chat_id`, `chat_type`, `thread_id`, `origin_json`). It also holds handoff
  and compression-failure state and the user flags `archived`, `pinned` and `hidden`.
- **`messages` rows.** Each is an OpenAI-format `role`/`content`/`tool_calls`, with reasoning
  sidecars and an `active` flag that marks the compaction generation. The `api_content` column holds
  the exact bytes sent to the provider when they differ from `content`, so replays hit the cache.
- **At most one live turn.** This is enforced by a durable cross-process turn lease.
- **Any number of warm runtimes, over time.** An `AIAgent` is built for the session in the CLI, in
  the gateway's agent cache (`gateway/run_agent_cache.py`) or in a `tui_gateway` session. Runtimes
  come and go without changing the conversation.

## Invariants

1. **The system prompt is byte-stable for the life of the session.** Nothing rebuilds it, swaps
   toolsets or reloads memory mid-session. Compression is the only exception. Content that must
   arrive mid-session rides a user message or a tool result. Slash commands that change what the
   prompt would contain take effect at the next session by default, with an opt-in `--now`. The
   reason is that the per-session prompt cache is what keeps long conversations affordable.
2. **Strict role alternation.** There are never two same-role messages in a row, and no synthetic
   user message is injected mid-loop. `/steer` is the only exception: a standalone user row after a
   tool result. Cron deliveries get their own session for this reason.
3. **Exactly one owning profile.** The session's rows live in its profile's `state.db`, and
   `profile_name` is stamped on creation. If the caller passes no `profile_name`, the store stamps
   its own. Any work on a session that runs outside a turn binds the *owner's* scope first. This
   covers memory flush, `on_session_end`, eviction, shutdown and title generation. The owner comes
   from the session record, never from `os.environ`; see [Profile](profile.md) invariants 4–5.
4. **One turn at a time, across processes.** `run_conversation` first takes a durable row lease
   (`agent/turn_facade_lease.py`, with a 300 s TTL that is refreshed). This lets the CLI, Desktop and
   the gateway share one session without interleaving turns.
5. **The durable transcript is the authority.** Warm in-memory history has to agree with
   `state.db`, not the reverse. Rewind (`/undo`, `/retry`) is an operation on persisted history
   (`hermes_state_rewind.py`).
6. **Time never ends a conversation.** There is no idle or daily reset, and the legacy
   `session_reset` config is ignored. Boundaries are only ever explicit (`/new`, `/reset`) or a
   suspension (`/stop`, stuck-loop escalation). A crash, restart, update or cache eviction resumes
   the same session.
7. **Compaction keeps the id.** With `compression.in_place: true` (the default), compacting
   archives the pre-compaction rows under the same id as `active=0, compacted=1`, and they stay
   searchable. An archived row can match a live row byte for byte, so never deduplicate one away.
   The legacy rotating path (`in_place: false`) ends the session as `compression` and continues it
   in a child session.

## Lifecycle

```
          first prompt / first message in a lane
                         │
                         ▼
   ┌──────────── active (ended_at NULL) ◄───────────────┐
   │     turns append messages; compaction archives    │
   │     old rows in place; /undo, /retry rewind       │ resume / reopen
   │                                                   │ (hermes -c, -r, /resume,
   ▼                                                   │  restart recovery)
 ended (ended_at + end_reason) ────────────────────────┘
   │                                                    only if the end was automatic
   ▼
 archived / pruned / deleted / exported
```

- **Create.** The row is created when there is first something to persist. TUI and Desktop drafts
  create no row until the first prompt, so unused drafts leave nothing behind.
- **End.** Setting `ended_at` and `end_reason` ends the *conversation*. Releasing a *runtime* is a
  separate event. End reasons fall into two classes, and `hermes_state_common.py` owns the
  taxonomy:
  - **Deliberate boundaries** (`_BOUNDARY_END_REASONS`): `new_session` (CLI `/new`),
    `session_reset` (gateway `/new` and `/reset`), `session_switch`, `suspended`, and others.
    Recovery never reopens a deliberate boundary.
  - **Automatic cleanup** (`is_automatic_end_reason`): `agent_close`, `ws_orphan_reap`,
    `superseded_by_resume`, `startup_orphan_reap`, `tui_shutdown`, `ws_disconnect`, `idle_timeout`,
    `lru_evict`. These mean that a runtime went away, and the conversation stays resumable.
- **Resume.** `hermes -c`, `hermes -r <id|title>`, `/resume`, or the gateway re-resolving a lane
  after a restart. Resuming restores the session's working directory. After a restart, the gateway
  picks the session with the most recent *actual activity* and respects `/new` boundaries.
- **Branch.** `/branch` creates a child session seeded from the parent. It is visible in pickers.
- **Handoff.** `/handoff <platform>` moves the *same* session id from the CLI to a messaging
  platform's home channel by re-binding that platform's lane to it.
- **Clean up.** `hermes sessions archive | prune | delete | export`. `prune` only touches sessions
  that have ended. `hermes sessions optimize` compacts storage without deleting anything.

## Lineage (`parent_session_id`)

A parent link means one of four things. `hermes_state_common.py` classifies them, and each kind is
treated differently:

| Kind | Created by | Marker | Visible in pickers? |
|---|---|---|---|
| Branch child | `/branch` | `model_config.$._branched_from` (legacy: parent `end_reason='branched'`) | Yes, as its own conversation |
| Reset child | Gateway `/new`, `/reset` | `model_config.$._reset_from` | Yes, as its own conversation |
| Compression continuation | Legacy rotating compaction | Parent `end_reason='compression'` | No: one logical conversation. It inherits the parent's gateway routing columns. |
| Subagent run | `delegate_task` | `source='subagent'` or `$._delegate_from` | No. It is excluded from the trigram index but still reachable by `session_search`. |

A child fills in missing `cwd`, git and `profile_name` values from its parent. `profile_name` is
only inherited within the same `agent:<ns>:` namespace.

## Sessions on messaging platforms

The gateway maps each inbound message to a lane with
`gateway/session.py::build_session_key`, then maps the lane to its current session id:

- **DMs** are always private: one session per DM chat.
- **Groups and channels** are isolated per user by default (`group_sessions_per_user: true`).
- **Threads and topics** are shared by all participants by default
  (`thread_sessions_per_user: false`).
- A **shared multi-user session** does not name one user in its system prompt. Instead it prefixes
  each user message with the sender's name, which keeps the prompt stable.
- Under a multiplexed gateway, the `agent:<profile>:` namespace keeps two profiles in the same chat
  from ever sharing a lane.

## Relationships

- **[Profile](profile.md) → Session (one to many).** A session never moves between profiles.
- **Session → Memory.** `MEMORY.md` and `USER.md` are read once when a session starts and stay
  frozen for that session. Memory extraction and the provider's `on_session_end` run when the
  session's runtime ends. This is why `/new` at natural stopping points is what makes the learning
  loop work. Cron sessions skip memory by default.
- **Session → Cron.** Each cron run is its own session (`source='cron'`).
- **Session → Subagent.** Each delegated run is a child session with the lineage kind shown above.

## Not to be confused with

- **Turn.** A turn is one user input followed by the agent loop until its final response. A session
  is a sequence of turns.
- **Context window.** This is what is sent to the model on a given call. After compaction it is
  much smaller than the session's full stored history.
- **Agent / runtime.** An `AIAgent` instance can be built, cached, evicted and rebuilt many times
  for a single session.
- **`sessions.json`.** A legacy mirror of the gateway routing index that contains only gateway
  lanes. It is not the list of sessions; `state.db` is.

## Code map

| Concern | Where |
|---|---|
| Store facade and topic siblings | `hermes_state.py` (`SessionDB`) + `hermes_state_*.py` |
| Schema | `hermes_state_common.py::SCHEMA_SQL` |
| Row creation and inheritance | `hermes_state_sessions.py` |
| Id minting | `hermes_state_ids.py` |
| Lineage and end-reason taxonomy | `hermes_state_common.py` (`_BOUNDARY_END_REASONS`, `is_automatic_end_reason`, `_*_CHILD_SQL`) |
| Rewind | `hermes_state_rewind.py` |
| Turn lease | `agent/turn_facade_lease.py` |
| Persistence from the agent | `agent/session_persistence.py` |
| Compaction | `agent/conversation_compression.py`, `agent/turn_context_compaction.py` |
| Gateway lanes, store, recovery | `gateway/session.py`, `gateway/session_recovery.py`, `gateway/run_agent_cache.py` |
| TUI/Desktop sessions | `tui_gateway/methods_session.py`, `tui_gateway/server.py` |
| CLI sessions | `hermes_cli/cli_session_mixin.py` |

## Further reading

- [Sessions (user guide)](../../website/docs/user-guide/sessions.md)
- [Session storage](../../website/docs/developer-guide/session-storage.md)
- [Gateway session lifecycle](../../website/docs/developer-guide/gateway-session-lifecycle.md)
- [Context compression and caching](../../website/docs/developer-guide/context-compression-and-caching.md)
- `agent/AGENTS.md` § Message-flow invariants
