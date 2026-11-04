# Matrix project routing — implementation plan

Status: **approved by the user (2026-11-03)** · Source HLD: `hermes-matrix-project-routing-hld.md` (2026-11-03)
· Base: `origin/main` @ `a81a714831` · Branch: `e2e-matrix-project-routing-hld`

## 0. Decisions (resolved with the user)

| # | Question | Decision |
|---|---|---|
| D1 | Scope | **HLD Phases 1+2.** A room binds to a project inside the profile that already serves the room (the receiving bot's owner, or a static `gateway.profile_routes` match). Dynamic `--profile` selection (Phase 3) and Phase 4 polish are **out of scope**. |
| D2 | Command surface | **Core built-in gateway command `/project`** (registry `gateway_only`) + `gateway/slash_commands_project.py` mixin. Element's `!project` works through the existing Matrix bang mapping (`_resolve_matrix_bang_command` → `is_gateway_known_command`). No plugin. |
| D3 | Authorization | **Follow the current admin logic.** `/project` is gated like every command (`_check_slash_access`); the mutating subcommands (`create`, `bind`, `unbind`) additionally require `SlashAccessPolicy.is_admin(user)`. As everywhere else, a platform scope with no `allow_admin_from` / `group_allow_admin_from` list treats every allowed user as admin (documented, with a recommendation to set the list). |
| D4 | E2E | **New Matrix C-S API stand-in + the real `hermes gateway run` child + `FakeLLMServer`.** Sandbox E2E steps go in `tests/AGENTS.md`; root `AGENTS.md` gets a compact pointer (it is at 11,994/12,000 chars). |

Designer's defaults (accepted by the user at approval):

| # | Topic | Default |
|---|---|---|
| D5 | Binding storage | Table `chat_project_bindings` in the **runtime profile's own `projects.db`**, next to the projects it references. A project and its binding are written in one DB, so there are no cross-DB partial failures, and profiles stay islands. Phase 3 can later add a gateway-owner routing layer on top. |
| D6 | Platform reach | **Platform-generic key** `(platform, transport_profile, room_id)` with `room_id = source.parent_chat_id or source.chat_id`. Matrix is the tested and documented target. Other platforms work through the same code path but are not separately E2E-tested. |
| D7 | Pin granularity | **Per conversation.** Each conversation pins its project once (stored in `SessionEntry.metadata`): `/new`, an auto-reset or a new thread picks up the room's *current* binding, while compaction keeps the pin. A conversation that already had history before the room was bound keeps running without a project until `/new`. |
| D8 | Rebind | `bind` over a different existing binding needs `--force`. Existing conversations keep their pin; nothing is migrated. |
| D9 | Unbind / delete | Unbind only stops routing *new* conversations. It deletes no files and no transcripts. A pinned or bound project that was deleted, archived, has no primary folder, or whose folder is missing **fails the turn with a clear reply**; the model is not called and there is no fallback. |
| D10 | Terminal backend | Project routing requires the runtime profile's `local` terminal backend. `bind`/`create` refuse otherwise, and a turn fails closed (see D9) if the backend changes later. |
| D11 | Scaffolding | `create` makes `<workspace_root>/<slug>/` with `AGENTS.md` (template, only if absent) and `sources/`. Nothing automates `STATUS.md`; `AGENTS.md` can instruct it. |
| D12 | Desktop grouping | Stamp the gateway session row's `sessions.cwd` with the project's primary path. Desktop already groups by `project_for_path(cwd)`, so no new index is needed (HLD open decision 5). |

## 1. Goal and acceptance criteria

**Goal.** From Element, an admin binds a Matrix room to a native Hermes Project (`projects.db`): `!project create <slug>` makes the workspace and binds it, and `!project bind <project>` binds an existing one. This needs no `config.yaml` edit and no gateway restart. Each new conversation in that room (each Matrix thread, or the room lane with `/new`) runs with the project folder as its working directory: `AGENTS.md`/context-file discovery, the system prompt's working directory, and terminal and file tools all use it. History stays isolated per thread, and replies go back to the originating room and thread.

**Acceptance criteria** (each one is proven by a test named in §4/§5):

- **AC1 – Per-room project context.** Rooms R1 and R2 are served by one profile and bound to projects A and B. Concurrent turns produce system prompts that contain only their own project's `AGENTS.md` marker, and a `pwd` terminal tool call returns their own workspace path.
- **AC2 – Thread isolation.** Two threads in R1 get distinct `session_id`s. Neither request's history contains the other thread's text, and both prompts contain A's marker.
- **AC3 – Delivery unchanged.** Every reply lands in the originating room, with an `m.thread` relation to the originating thread root, from the receiving bot.
- **AC4 – Live management.** `create`/`bind`/`info`/`list`/`unbind` take effect for the next new conversation while the gateway PID stays the same.
- **AC5 – Restart persistence.** After a gateway stop/start, an existing R1 thread continues in A (marker, `pwd`) and replies in the same thread. The `sessions.cwd` row value equals A's primary path.
- **AC6 – Rebind semantics.** `bind B` in a room bound to A is refused without `--force`. After `--force`, existing R1 threads stay in A and a new thread runs in B.
- **AC7 – Authorization and path safety.** With an admin list configured, a non-admin is refused `create`/`bind`/`unbind` but may run `info`/`list` if allowed `/project`. `create` refuses an unset `workspace_root`, an invalid slug (separators or `..`), and a symlink escape out of the root.
- **AC8 – Fail closed.** A bound or pinned project that is missing, archived, has no folder, or sits on a non-local backend gets a clear reply. The fake model receives no request for that message, and no other project's or the default context is used.
- **AC9 – Unbound rooms unchanged.** With no binding, the turn's cwd, prompt and static `profile_routes` behave exactly as on `main` (existing gateway suites stay green).
- **AC10 – No leakage.** Concurrent turns with different pins each see their own `resolve_agent_cwd()`, `resolve_context_cwd()` and `terminal_env("TERMINAL_CWD")`, and the scope is restored afterwards (unit, thread-concurrent).
- **AC11 – Pre-existing conversations.** A conversation with history from before the room was bound keeps running without a project; `/new` picks up the binding.

## 2. Out of scope

- Dynamic `--profile` selection, profile allowlists, and lane-level profile pins (HLD Phase 3).
- `/project sessions`, project→room notification targets, cron delivery, and project-scoped memory/search (Phase 4).
- Non-`local` terminal backends (D10).
- Migrating an existing conversation into a new project. Users start a new thread or run `/new`.

## 3. Design

### 3.1 Data model

**Binding** (runtime profile's `$HOME/projects.db`, added to `projects_db.SCHEMA_SQL`):

```sql
CREATE TABLE IF NOT EXISTS chat_project_bindings (
    platform          TEXT NOT NULL,   -- Platform.value, e.g. "matrix"
    transport_profile TEXT NOT NULL,   -- RoutingIdentity.transport_profile ("default" spelled out)
    chat_id           TEXT NOT NULL,   -- stable room id: parent_chat_id or chat_id (never an alias/name)
    project_id        TEXT NOT NULL,   -- projects.id in THIS db; deliberately no FK (see below)
    bound_by          TEXT,            -- sender user id, audit only
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    PRIMARY KEY (platform, transport_profile, chat_id)
);
```

There is no `REFERENCES`. With `foreign_keys=True`, a cascade would silently unbind a room when its project is deleted (violating D9), and RESTRICT would block `hermes project delete`. A dangling `project_id` is reported at lookup instead.

**Conversation pin** (`SessionEntry.metadata["project_pin"]`, persisted with the routing entry in state.db/sessions.json):

```json
{"project_id": "p_1a2b3c4d" | null, "slug": "alpha", "name": "Alpha", "cwd": "/abs/projects/alpha",
 "profile": "default", "pinned_at": 1793750400}
```

`project_id: null` is an explicit "this conversation runs without a project". It stops a later binding from hijacking that conversation.

### 3.2 Inbound data flow (messaging turn)

```
Matrix event → adapter (unchanged; !project → /project) → canonicalize identity (unchanged)
  ├─ slash /project → GatewayProjectCommandsMixin._handle_project_command  (§3.5)
  └─ chat turn → get_or_create_session (key derivation unchanged)
       → _hmwa_prepare_turn: open session, _set_session_env, lease, load transcript (+hygiene)
       → NEW: apply_turn_project_pin(runner, source, entry, history)
            in a worker thread under runner._profile_scope_for_source(source):
              resolve_turn_pin(...)  → (pin, persist)   | ProjectRouteError → refusal reply, return
            persist → session_store.set_session_metadata(key, "project_pin", pin.to_dict())
                      + (bound) session_store.update_session_row_cwd(key, session_id, pin.cwd)
       → _run_agent (enters profile scope) → executor → TurnRunner.run_sync
            NEW: ExitStack also enters project_turn_scope(project_pin_for_key(runner, key))
                 (only when policy is None, i.e. not a LOCAL desktop session)
              → set_session_cwd(pin.cwd) + terminal scope overlay TERMINAL_CWD=pin.cwd
            → _resolve_turn_agent: signature includes session_cwd → AIAgent(cwd=pin.cwd)
              → system prompt / AGENTS.md discovery / subdirectory hints / terminal & file tools
       → delivery (unchanged: receiving adapter, same room/thread)
```

Order matters. The pin is resolved before the agent is built or reused. The cwd scope is entered inside the profile scope, because `_profile_runtime_scope` installs the profile terminal policy and would overwrite an earlier overlay. The scope is task-local (contextvars), and nothing calls `os.chdir` or writes `os.environ`.

### 3.3 `resolve_turn_pin` rules

| Existing pin | History | Result | Persist |
|---|---|---|---|
| bound | any | revalidate (project exists, not archived, `pin.cwd` is a dir, local backend) → same pin, or raise | no |
| unbound (`null`) | any | run with no project | no |
| absent | non-empty | `UNBOUND` (pre-existing conversation, AC11) | yes |
| absent | empty | room binding → validated pin; no binding → `UNBOUND`; invalid binding → raise (nothing persisted, so the next message retries) | yes |

"History empty" (the transcript loaded for this turn) is used instead of `_is_new_session`. If a first turn fails, the conversation stays un-pinned and empty, so the next message resolves the binding again rather than silently running without a project.

### 3.4 Module boundaries and interfaces

**`hermes_cli/projects_db.py`** (extend; currently 485 lines):

```python
@dataclass(frozen=True)
class ChatBinding:
    platform: str
    transport_profile: str
    chat_id: str
    project_id: str
    bound_by: Optional[str]
    created_at: int
    updated_at: int

def get_chat_binding(conn: sqlite3.Connection, *, platform: str, transport_profile: str,
                     chat_id: str) -> Optional[ChatBinding]: ...
def set_chat_binding(conn: sqlite3.Connection, *, platform: str, transport_profile: str, chat_id: str,
                     project_id: str, bound_by: Optional[str] = None) -> ChatBinding:
    """Upsert (created_at preserved on update). ValueError if project_id is not in `projects`."""
def delete_chat_binding(conn: sqlite3.Connection, *, platform: str, transport_profile: str,
                        chat_id: str) -> bool: ...
```

All writes go through `write_txn` with parameterized SQL. Callers always pass an explicit DB path to `connect_closing(db_path=...)`.

**`gateway/project_routing.py`** (new; pure routing model with no runner state beyond duck-typed reads):

```python
PIN_METADATA_KEY = "project_pin"

@dataclass(frozen=True)
class ProjectPin:
    project_id: Optional[str]            # None = conversation runs without a project
    slug: str = ""
    name: str = ""
    cwd: str = ""
    profile: str = ""
    pinned_at: int = 0
    @property
    def bound(self) -> bool: ...
    def to_dict(self) -> dict[str, Any]: ...
    @classmethod
    def from_dict(cls, data: Any) -> Optional["ProjectPin"]: ...   # None for malformed/absent

UNBOUND_PIN: ProjectPin  # ProjectPin(project_id=None)

class ProjectRouteError(RuntimeError):
    """Fail-closed routing failure; `reason` in {no_identity, missing, archived, no_path,
    path_missing, backend}; `user_message()` renders t(f"gateway.project.error.{reason}", **params)."""
    def __init__(self, reason: str, **params: str) -> None: ...
    reason: str
    params: dict[str, str]
    def user_message(self) -> str: ...

def binding_chat_id(source: SessionSource) -> str            # parent_chat_id or chat_id
def binding_key(runner: Any, source: SessionSource) -> tuple[str, str, str]
    # (platform, transport_profile, chat_id). transport = identity_of(source).transport_profile;
    # no identity + not multiplexed → runner._primary_profile_name or "default";
    # no identity + multiplexed → ProjectRouteError("no_identity").
def runtime_projects_db(runner: Any, source: SessionSource) -> Path
    # Path(runner._resolve_profile_home_for_source(source)) / "projects.db"  (identity-aware; works
    # for restored/internal sources too). Never projects_db_path() (task-local home).
def require_local_backend() -> None                          # terminal_env("TERMINAL_ENV") in ("", "local")
def validate_project(project: Optional[Project], ref: str) -> str
    # returns the normalized primary path; raises missing/archived/no_path/path_missing
def pin_from_binding(runner: Any, source: SessionSource, *, now: Optional[int] = None) -> ProjectPin
def revalidate_pin(runner: Any, source: SessionSource, pin: ProjectPin) -> None
def resolve_turn_pin(runner: Any, source: SessionSource, existing: Optional[ProjectPin], *,
                     history_empty: bool) -> tuple[ProjectPin, bool]   # (pin, persist) per §3.3

@contextmanager
def project_turn_scope(pin: Optional[ProjectPin]) -> Iterator[None]:
    """No-op for None/unbound. Else set_session_cwd(pin.cwd) and set_terminal_scope(overlay) where
    overlay = {**base, "TERMINAL_CWD": pin.cwd}, base = get_terminal_scope() or
    prepare_profile_terminal_scope(get_hermes_home()) (standalone gateways bind no scope — a bare
    {"TERMINAL_CWD": ...} mapping would hide every other TERMINAL_* key); a TerminalPolicyRefusal base
    is installed unchanged (enforce_no_refusal must still fire). Reset both tokens in finally."""
```

**`gateway/project_routing_turn.py`** (new; runner glue):

```python
def project_pin_for_key(runner: Any, session_key: Optional[str]) -> Optional[ProjectPin]
    # ProjectPin.from_dict(runner.session_store.get_session_metadata(key, PIN_METADATA_KEY))
async def apply_turn_project_pin(runner: Any, source: SessionSource, session_entry: SessionEntry,
                                 history: list) -> Optional[str]:
    """Resolve + persist this conversation's pin before the agent runs; returns refusal text or None.
    Skips Platform.LOCAL (desktop sessions own their workspace via session policy). Resolution runs
    in asyncio.to_thread inside runner._profile_scope_for_source(source) (sync CM entered in the
    worker, like _reset_notice_session_info). Logs a WARNING with reason + room on refusal."""
```

**`gateway/session_persistence.py`** (SessionStore mixin; add a public seam beside `_db_for_key`):

```python
def update_session_row_cwd(self, session_key: str, session_id: str, cwd: str) -> None:
    """Stamp sessions.cwd (Desktop project grouping, D12) via the key's own SessionDB; failures
    are logged, never raised (row metadata only)."""
```

**Edits in existing turn code:**

- `gateway/run_turn_prepare.py::_hmwa_prepare_turn`: right after the transcript/hygiene `try` block, add `refusal = await apply_turn_project_pin(self, source, session_entry, history)`. When `refusal` is set, call `self._clear_session_env(_session_env_tokens)` and return `record_unexecuted_failure(refusal, reason="project_route"), _session_env_tokens`, mirroring the `TranscriptReadError` path exactly.
- `gateway/run_turn_runner.py::TurnRunner.run_sync`: in the `else:` branch after `api_policy_scope()`, add `if policy is None: scope.enter_context(project_turn_scope(project_pin_for_key(self._runner, self._ctx.session_key)))`.
- `gateway/session_execution.py`: `_resolve_turn_agent` reads the pin once and passes `session_cwd=pin.cwd if pin and pin.bound else ""` to `_agent_config_signature`. `_build_fresh_agent` passes `cwd=<same or None>` to `AIAgent`.
- `gateway/run_agent_cache.py::_agent_config_signature(..., skip_context_files: bool = False, session_cwd: str = "")`: add `session_cwd` to the hashed blob, with a WHY comment explaining that a different workspace changes the frozen prompt.
- `gateway/run_background.py::_run_background_task_inner`: get `pin = project_pin_for_key(self, self._session_key_for_source(source))`. If the pin is bound, `revalidate_pin` it (a `ProjectRouteError` sends `user_message()` and returns). Wrap `run_sync`'s agent run in `project_turn_scope(pin)` and pass `AIAgent(cwd=...)`.

**`gateway/slash_commands_project.py`** (new mixin `GatewayProjectCommandsMixin`, composed into `GatewaySlashCommandsMixin` in `gateway/slash_commands.py`; `"project"` added to `_IDLE_COMMANDS` in `gateway/run_busy.py`):

```python
class GatewayProjectCommandsMixin:
    async def _handle_project_command(self, event: MessageEvent) -> str: ...
    # subcommand table {"": info, "info", "list", "create", "bind", "unbind", "help"} → bound
    # coroutine; no if/elif ladder. Args: shlex.split(event.get_command_args()); flags --force, --name.
```

- DB and filesystem work runs in `asyncio.to_thread` under `async with self._async_profile_scope_for_source(source)`. That binds the runtime profile (#119915), so `require_local_backend()` reads the right policy. DB paths are explicit (`runtime_projects_db`).
- Mutations (`create`, `bind`, `unbind`) call `gateway.slash_access.policy_for_runner_source(self, source).is_admin(source.user_id)` (D3); a refusal uses `gateway.project.admin_only`.
- `info` shows the room binding (project name/slug/id/path/profile, or "unbound", or a dangling-reference error) and the current conversation's pin from `project_pin_for_key(self, self._session_key_for_source(source))`. When the two differ it adds a note: "this conversation started before the rebind; /new or a new thread uses <new>".
- `list` shows non-archived projects in the runtime profile's `projects.db`, marking the room's bound one.
- `bind <id|slug> [--force]`: `get_project` → `validate_project` → `require_local_backend` → existing binding. Same project gives `already_bound`. A different project without `--force` gives `rebind_needs_force`, naming the current one. Otherwise `set_chat_binding(bound_by=user_id)`, replying with the path and the D7/D8 note.
- `create <slug> [--name "Display"] [--force]`:
  - Root: `GatewayConfig.project_workspace_root`. If it's unset, reply `no_workspace_root`. It must be an existing directory after `expanduser`; resolve it with `realpath`.
  - Target: `normalize_slug` (a `ValueError` gives `invalid_slug`), then `target = root/slug`. If `target.is_symlink()` or `target.resolve().parent != root`, reply `unsafe_path`. Then `mkdir(exist_ok=True)`, write `AGENTS.md` only if absent, and `mkdir sources/`.
  - Project, idempotently: if the slug exists with the same primary path, reuse it. If it exists with a different path, reply `slug_conflict` (suggesting `bind`). Else, if `find_by_primary_path(target)` matches, reuse that. Else `create_project(name=name or slug, slug=slug, primary_path=target)`.
  - Then bind, with the same `--force` rule.
  - Partial failure is safe: a retry reuses the project.
- `unbind` calls `delete_chat_binding`, replying `unbound` or `not_bound`. Files and transcripts stay untouched.
- `help` and malformed input reply `usage`.
- Local (`Platform.LOCAL`) sessions never reach this handler: `slash.exec` refuses non-read commands, so the command is messaging-only.

**Command registry** (`hermes_cli/commands.py`):

```python
CommandDef("project", "Bind this room to a project workspace (info/list/create/bind/unbind)", "Session",
           gateway_only=True, args_hint="[info|list|create <slug>|bind <project> [--force]|unbind]")
```

If the Slack 50-command cap test trips, add `"project"` to `_SLACK_VIA_HERMES_ONLY` in `hermes_cli/commands_platforms.py`, since it's a rare admin command. Regenerate `website/docs/developer-guide/gateway-command-parity.md` with `python scripts/gen_gateway_command_parity.py`.

**Config** (gateway section; there's no env var, per the rubric):

```yaml
gateway:
  project_routing:
    workspace_root: ""   # absolute dir where `/project create` makes workspaces; "" = create disabled
```

- `hermes_cli/config_defaults.py`: add the key to `DEFAULT_CONFIG["gateway"]` with a short comment. The file is over the line ratchet, so **net line growth must be ≤ 0**: compact neighbouring comment lines in the same gateway block, as recent commits did.
- `hermes_cli/config.py`: add `"project_routing"` to the known gateway keys list (line ~963).
- `gateway/config.py`: add a `GatewayConfig.project_workspace_root: Optional[str] = None` field, parsed from `data.get("project_routing") or {}` with a `.get("workspace_root")` that is stripped, and empty → `None`.
- The gateway reads its own config (the launch or default root). There's no per-profile root in this scope.

**i18n.** Every new key goes into all 17 `locales/<lang>.yaml` (parity test `tests/agent/test_i18n.py`), with placeholders matching the English text:

- T1 owns `gateway.project.error.{no_identity,missing,archived,no_path,path_missing,backend}`.
- T4 owns `commands.project.description` and `gateway.project.{usage,admin_only,info_bound,info_unbound,info_dangling,info_pin,info_pin_differs,list_header,list_item,list_empty,created,reused,bound,already_bound,rebind_needs_force,unbound,not_bound,no_workspace_root,invalid_slug,unsafe_path,slug_conflict,unknown_project}`. The final names are the implementer's call; the parity test is the contract.

### 3.5 Error handling and conventions

- **Fail closed.** `ProjectRouteError` becomes a user-visible reply plus one WARNING log line (reason, platform, room). The turn is never run with another context.
- **SQLite access.** Transactions are short (`write_txn`), connections use `connect_closing`, and the gateway loop only does SQLite work through `asyncio.to_thread`.
- **Profile scope.** Resolution runs under `runner._profile_scope_for_source`, and slash work under `_async_profile_scope_for_source`. Paths come from `_resolve_profile_home_for_source` / identity, never `os.environ` or `projects_db_path()`. That satisfies the HX002/HX012-class rules.
- **Prompt caching.** The cwd only changes at conversation boundaries (the pin is immutable within a conversation), and the agent cache signature includes the cwd. Nothing mutates the system prompt mid-conversation.
- **Code health.**
  - New functions: CC ≤ 20, ≤ 300 lines. Use a subcommand table, not a ladder.
  - `gateway/run.py` is not touched.
  - `config_defaults.py` has net growth ≤ 0.
  - Comments explain WHY only.
  - No `try/except: pass`.
- **Tests** run via `scripts/run_tests.sh` only. They are behaviour contracts with a temp `HERMES_HOME` (autouse), with no source-reading or count snapshots.

## 4. Tasks and waves

The environment comes first and is required for E2E and the `mautrix` import:
`python -m pm.build_env --source . --out .venv --group dev --group test --extra matrix`

### Wave 1 (parallel)

**T1 — Binding store + routing model** · *subtle*

- **Goal:** the binding CRUD, the pin model, the §3.3 resolution rules, validation, and `project_turn_scope`, plus the error i18n keys.
- **Owns:**
  - `hermes_cli/projects_db.py`
  - `gateway/project_routing.py` (new)
  - `locales/*.yaml` (only the `gateway.project.error.*` keys)
  - `tests/hermes_cli/test_projects_db_bindings.py` (new)
  - `tests/gateway/test_project_routing.py` (new)
- **Produces:** the `projects_db` binding API and everything in `gateway/project_routing.py` (§3.4).
- **Consumes:** `projects_db` (`get_project`, `Project`), `gateway.session_identity.identity_of`, `agent.runtime_cwd.{set,reset}_session_cwd`, `tools.terminal_scope.{get_terminal_scope,set_terminal_scope,reset_terminal_scope,prepare_profile_terminal_scope,terminal_env,TerminalPolicyRefusal}`, `agent.i18n.t`.
- **Depends:** none.
- **Tests must cover:**
  - CRUD/upsert keeps `created_at`; unknown project → `ValueError`; deleting a project leaves a dangling binding, which `validate_project` reports as `missing`; a legacy DB upgrades in place.
  - Every row of the §3.3 table.
  - Each error reason, and a non-local backend.
  - `project_turn_scope` (AC10): `resolve_agent_cwd`, `resolve_context_cwd` and `terminal_env("TERMINAL_CWD")` return the pin cwd inside and the prior values after. Two threads with different pins concurrently each see their own. With no scope bound, `terminal_env("TERMINAL_ENV")` is unchanged by the overlay. A refusal base still raises in `enforce_no_refusal`.
  - `binding_key` uses `parent_chat_id` for threads and rejects identity-less multiplexed sources.
- **Unit tests:** `scripts/run_tests.sh tests/hermes_cli/test_projects_db_bindings.py tests/gateway/test_project_routing.py tests/agent/test_i18n.py`

**T2 — Matrix C-S API stand-in + smoke E2E** · *subtle*

- **Goal:** a local Matrix homeserver stand-in that the real `plugins/platforms/matrix` adapter (mautrix) talks to, following the `tests/fakes/platforms/_standin.py` pattern. It records calls, queues faults, and answers unknown endpoints `404 M_UNRECOGNIZED`, recorded as faulted.
- **Owns:**
  - `tests/fakes/platforms/matrix_standin.py` (new)
  - `tests/e2e/core/platforms/_drv_matrix.py` (new; config/env helpers for `GatewayUnderTest`)
  - `tests/e2e/core/platforms/test_matrix_standin_smoke.py` (new)
- **Endpoints, minimum** (the set mautrix and the adapter call on connect, sync, send, typing and receipts; add whatever the log shows is missing):
  - `GET /_matrix/client/versions`, `GET /v3/account/whoami`, `POST /v3/user/{u}/filter`
  - `GET /v3/sync`: a long-poll honouring `since`/`timeout`, carrying `rooms.join.*.{state,timeline}` with `m.room.member`/`m.room.name` state and `next_batch`
  - `PUT /v3/rooms/{r}/send/{type}/{txn}`, `GET /v3/rooms/{r}/state/{type}[/{key}]`, `GET /v3/rooms/{r}/joined_members`, `GET /v3/rooms/{r}/members`
  - `PUT /v3/rooms/{r}/typing/{u}`, `POST /v3/rooms/{r}/receipt/{type}/{ev}`, `POST /v3/rooms/{r}/read_markers`
  - `PUT /v3/presence/{u}/status`, `GET|PUT /v3/user/{u}/account_data/{type}`, `GET /v3/profile/{u}[/displayname]`, `POST /v3/join/{r}`
- **Test API:**
  - `add_room(room_id, name, members)`
  - `post(room_id, sender, body, *, thread_root=None) -> event_id`
  - `bot_messages(room_id, *, thread_root=None) -> list[dict]`: visible final text after `m.replace` edits, plus the relation
  - `calls(method)`
- **Smoke test** (real gateway child + `FakeLLMServer`):
  - The adapter connects.
  - A room message gets the scripted reply in that room.
  - A threaded message's reply carries `m.relates_to.rel_type == "m.thread"` with the right root.
  - Markers: `pytest.mark.platforms("linux")` (mautrix is linux-only) + `spawns_gateway_lookalike`, plus `pytest.importorskip("mautrix")` with a reason pointing at the `--extra matrix` env.
- **Depends:** none.
- **Unit tests:** `scripts/run_tests.sh tests/e2e/core/platforms/test_matrix_standin_smoke.py -v`

### Wave 2 (parallel; T3 and T4 own disjoint files)

**T3 — Turn wiring** · *subtle*

- **Goal:** wire the pin into the messaging turn and `/background` (§3.2, §3.4 "Edits in existing turn code"), stamp `sessions.cwd`, and add the cwd to the agent cache signature.
- **Owns:**
  - `gateway/project_routing_turn.py` (new)
  - `gateway/run_turn_prepare.py`, `gateway/run_turn_runner.py`, `gateway/session_execution.py`, `gateway/run_agent_cache.py`, `gateway/run_background.py`, `gateway/session_persistence.py`
  - `tests/gateway/test_project_turn_wiring.py` (new)
- **Consumes (T1):** `ProjectPin`, `PIN_METADATA_KEY`, `resolve_turn_pin`, `revalidate_pin`, `project_turn_scope`, `ProjectRouteError`.
- **Produces:** `project_pin_for_key`, `apply_turn_project_pin`, `SessionStore.update_session_row_cwd`, `_agent_config_signature(session_cwd=)`.
- **Depends:** T1.
- **Tests** (real `SessionStore` + `SessionDB` on a temp home with a real `projects.db`; a stub runner is fine where the full `GatewayRunner` is unnecessary):
  - A first message in a bound room persists the pin and stamps `sessions.cwd`; an unbound room persists `UNBOUND`.
  - A pinned conversation keeps its project after a rebind; `/new` (`force_new`) re-resolves.
  - A refusal returns text without persisting.
  - `project_pin_for_key` survives a `SessionStore` reload from disk (restart, AC5).
  - The signature differs by `session_cwd`.
  - Inside `run_sync`'s stack, `resolve_agent_cwd()` equals the pin cwd for a messaging source, while a LOCAL policy source is unaffected.
- **Unit tests:** `scripts/run_tests.sh tests/gateway/test_project_turn_wiring.py tests/gateway/` (the whole gateway dir, for AC9 regressions)

**T4 — `/project` command, config, i18n, docs**

- **Goal:** the management command (§3.4), the config key, the registry entry, the command i18n keys, the parity doc, and the user docs.
- **Owns:**
  - `gateway/slash_commands_project.py` (new), `gateway/slash_commands.py`, `gateway/run_busy.py`
  - `hermes_cli/commands.py`, `hermes_cli/commands_platforms.py` (only if the Slack cap trips)
  - `hermes_cli/config_defaults.py`, `hermes_cli/config.py`, `gateway/config.py`
  - `locales/*.yaml` (the `commands.project.*` and non-error `gateway.project.*` keys)
  - `website/docs/developer-guide/gateway-command-parity.md` (regenerated)
  - `website/docs/reference/slash-commands.md`
  - `website/docs/user-guide/messaging/matrix.md`: a new "Project rooms" section covering create/bind/info/list/unbind, `workspace_root`, the threads guidance (`MATRIX_SESSION_SCOPE=room` + `MATRIX_AUTO_THREAD=false` for manual threads; ≤2-member rooms are DMs), the rebind/`/new` semantics, the admin recommendation, and the "cwd is not a sandbox" note
  - The config reference page that documents `gateway.profile_routes` (locate it with `git grep -l profile_routes website/docs`)
  - `tests/gateway/test_project_command.py` (new)
- **Consumes (T1):** the `projects_db` binding API, `binding_key`, `runtime_projects_db`, `validate_project`, `require_local_backend`, `ProjectPin`, `PIN_METADATA_KEY`, `ProjectRouteError`.
- **Consumes (existing):** `policy_for_runner_source`, `_async_profile_scope_for_source`, `_session_key_for_source`, `session_store.get_session_metadata`.
- **Note:** `project_pin_for_key` is T3's; to stay independent, T4 reads the metadata directly through `ProjectPin.from_dict(self.session_store.get_session_metadata(key, PIN_METADATA_KEY))`.
- **Depends:** T1.
- **Tests:**
  - Each subcommand against a real `projects.db` in a temp home, through the real handler.
  - AC7: admin gating with `group_allow_admin_from` set; unrestricted when unset (D3).
  - `create` refusals for an unset root, an invalid slug, and a symlink escape; idempotent re-create; a slug conflict.
  - `bind` refused without `--force` when bound elsewhere.
  - `info` reports a dangling binding.
  - The Matrix normalization `_normalize_matrix_bang_command("!project bind x") == "/project bind x"`.
  - The config invariant: `workspace_root` set in a temp `config.yaml` reaches `GatewayConfig.project_workspace_root` through `load_gateway_config`.
- **Unit tests:** `scripts/run_tests.sh tests/gateway/test_project_command.py tests/hermes_cli/test_gateway_command_parity.py tests/hermes_cli/ tests/agent/test_i18n.py`

### Wave 3

**T5 — Feature E2E journey + sandbox E2E instructions**

- **Goal:** the §6 journey as one test module, and the AGENTS.md sandbox-E2E gate.
- **Owns:**
  - `tests/e2e/core/platforms/test_matrix_project_routing.py` (new)
  - `tests/AGENTS.md` (new section, §6.2)
  - `AGENTS.md` (root; the compact pointer, staying ≤ 12,000 chars by tightening wording elsewhere without dropping any rule; verify with `python scripts/check`)
- **Consumes:** T2's stand-in/driver; T3 and T4 behaviour.
- **Depends:** T2, T3, T4.
- **Unit tests:** `scripts/run_tests.sh tests/e2e/core/platforms/test_matrix_project_routing.py -v`

## 5. Test and verification (whole feature)

```bash
python -m pm.build_env --source . --out .venv --group dev --group test --extra matrix
scripts/run_tests.sh tests/hermes_cli/test_projects_db_bindings.py tests/gateway/test_project_routing.py \
    tests/gateway/test_project_turn_wiring.py tests/gateway/test_project_command.py
scripts/run_tests.sh tests/gateway/ tests/hermes_cli/ tests/agent/test_i18n.py tests/plugins/   # regressions (AC9)
scripts/run_tests.sh tests/e2e/core/platforms/test_matrix_standin_smoke.py \
    tests/e2e/core/platforms/test_matrix_project_routing.py -v                                  # E2E (AC1–AC8, AC11)
python scripts/check            # every blocking CI lint: ruff, code-health ratchet (file/func caps,
                                # config_defaults net growth), profile-scope patterns, config writers,
                                # AGENTS.md size caps (root ≤12k, chains ≤30k)
python scripts/gen_gateway_command_parity.py && git diff --exit-code website/docs/developer-guide/gateway-command-parity.md
```

- **Type checks:** whatever `scripts/check` runs; the new modules carry full annotations.
- **Build:** no TS or packaging changes. If the docs build is available in the sandbox, `cd website && npm run build` catches broken links in the edited pages; otherwise rely on the parity test.

## 6. E2E journey (sandbox, feature branch)

### 6.1 Journey

`tests/e2e/core/platforms/test_matrix_project_routing.py` runs everything in-process from pytest:

- The real `hermes gateway run` child runs on a throwaway HOME (`GatewayUnderTest`).
- `MatrixStandin` stands in for the homeserver.
- `FakeLLMServer` is driven by a `Director`: per-`[in:<token>]` scripts, recording every request.

**Setup.**

- Stand-in rooms: `!alpha:standin.local` and `!beta:standin.local`, each with members `@admin`, `@guest` and `@hermes` (3 members, so they're group rooms, not DMs).
- Env: `MATRIX_ALLOWED_USERS=@admin:…,@guest:…`, `MATRIX_E2EE_MODE=off`, `MATRIX_REQUIRE_MENTION=false`, `MATRIX_SESSION_SCOPE=room`, `MATRIX_AUTO_THREAD=false`.
- Config: `platforms.matrix.extra.group_allow_admin_from: ["@admin:standin.local"]`, `gateway.project_routing.workspace_root: <tmp>/projects` (pre-created), terminal backend `local`.
- Before start: run `hermes project create Beta <tmp>/existing/beta --slug beta` in the child env. `AGENTS.md` there contains `BETA-MARKER`.

| # | User step (Element equivalent) | Expected result |
|---|---|---|
| 1 | Start gateway | Adapter connected to the stand-in; PID recorded |
| 2 | `@admin` in !alpha: `!project create alpha` | Reply names `alpha` and `<tmp>/projects/alpha`; the dir holds `AGENTS.md` + `sources/`; the test appends `ALPHA-MARKER` to that `AGENTS.md` |
| 3 | `@admin` in !beta: `!project bind beta` | Reply confirms `beta` with its path |
| 4 | `@guest` in !alpha: `!project unbind` | Admin-only refusal; `!project info` still shows `alpha` (AC7) |
| 5 | `@admin` in !alpha thread T1 `[in:a1] pwd?` and, without waiting, in !beta thread U1 `[in:b1] pwd?`; the model answers each with `terminal(pwd)` then text | A1's request system prompt has `ALPHA-MARKER`, not `BETA-MARKER`; the tool result is the alpha path. B1 is the mirror image. Each reply arrives in its own room as an `m.thread` reply to T1/U1 (AC1, AC3, AC10) |
| 6 | !alpha thread T2 `[in:a2] hello` | Different `session_id` from T1; the request history has no T1 text; the prompt has `ALPHA-MARKER` (AC2) |
| 7 | `!project bind beta` in !alpha | Refused: already bound to alpha, needs `--force` |
| 8 | `!project bind beta --force` in !alpha, then T1 `[in:a3] pwd?`, then new !alpha thread T3 `[in:a4] pwd?` | T1 is still alpha (marker + `pwd`); T3 is beta. The gateway PID is unchanged (AC4, AC6) |
| 9 | Stop and start the gateway; T1 `[in:a5] pwd?` | alpha marker + path; the reply is in T1. `state.db` `sessions.cwd` for T1's session is the alpha path (AC5) |
| 10 | In the child env, `hermes project archive beta`; new !beta thread U2 `[in:b2] hi` | The reply names `beta` as archived; the fake model received **no** request for `b2` (AC8) |
| 11 | `@admin` in !beta: `!project unbind`, then new thread U3 `[in:b3] pwd?` | The reply confirms unbound. B3's prompt has neither marker, and `pwd` is the profile's default terminal cwd (AC9) |
| 1b | (Between steps 1 and 2) `@admin` posts `[in:p0] hi` in the !alpha room timeline, with no thread, while !alpha is unbound | Normal reply; this timeline conversation now has history |
| 3b | (Between steps 3 and 4, with !alpha bound to alpha) timeline `[in:p1] pwd?`, then `!new`, then timeline `[in:p2] pwd?` | p1's prompt has no marker and `pwd` is the default cwd (a pre-existing conversation, AC11). p2's prompt has `ALPHA-MARKER` and `pwd` is the alpha path (`/new` picks up the binding, D7) |

### 6.2 Sandbox E2E instructions (proposed text)

**`tests/AGENTS.md`, new section `## Sandbox feature E2E`:**

> A feature E2E is one user-journey module under `tests/e2e/core/` that drives the REAL
> `hermes gateway run` child (`tests/e2e/core/platforms/_helpers.py::GatewayUnderTest`: start, stop and
> restart on the same throwaway HOME) against a local platform stand-in (`tests/fakes/platforms/*_standin.py`)
> and the recording fake model (`tests/fakes/fake_llm_provider.py`). It never runs against the
> developer's live `~/.hermes`, a real chat network or a real provider. Build the env with the stand-in
> platform's extra, then run the module:
>
> ```bash
> python -m pm.build_env --source . --out .venv --group dev --group test --extra matrix
> scripts/run_tests.sh tests/e2e/core/platforms/test_matrix_project_routing.py -v
> ```
>
> Assert on what the stand-in delivered and what the fake model received (prompts, tool results), not
> on log text. A module needing an optional SDK `importorskip`s it with a reason naming the extra.

**Root `AGENTS.md`:** one clause in the existing **Tests** bullet of "Rules that apply everywhere":

> `… tests never write to ~/.hermes/. Feature E2E (sandbox, real gateway + stand-ins): tests/AGENTS.md.`

Trim an equal number of characters elsewhere in the root, compressing wording only, so it stays ≤ 12,000 chars.

## 7. Risks and notes for implementers

- **T2 (stand-in) is the long pole.** mautrix may call endpoints that aren't listed here. Run the smoke test, read the gateway log for 404s, and add them. Never weaken the adapter to fit the stand-in.
- **The terminal scope overlay in standalone mode (T1)** is the easiest place to regress existing behaviour. The "no scope bound" test is mandatory.
- **Slash dispatch** runs under the *receiving* bot's scope. Every DB or backend read in `/project` must happen inside `_async_profile_scope_for_source`, or it reads the wrong profile under multiplexing.
- **AGENTS.md hard caps:** root ≤ 12k, root→area chains ≤ 30k (`scripts/check`).
- **Open note for later phases:** Phase 3 would add a gateway-owner routing layer resolved before key derivation (the profile is in the session-key namespace), plus lane-level profile pins. This plan's per-profile table and per-conversation pin stay valid underneath it.
