# Matrix command descriptions (MSC4391 publisher) — implementation plan

Status: **approved by the user (2026-11-09)** · branch `feat/matrix-command-descriptions` · base
`db722732ad1e7002cd06fc3c813a109626466d59` (`refs/heads/main`) · worktree
`/home/sinkerine/.paseo/worktrees/15zkmyhp/matrix-command-descriptions`.

Read-only references (never edited): Element checkout `/home/sinkerine/dev/element-web`
(`develop` @ `d698eb223e7dc3030fed2c49330e9ecb98b4ab0d`), its plan
`docs/features/hermes-slash-plan.md` §2.7, and the consumer code
`apps/web/src/slash-commands/bot-commands/{BotCommandProtocol,RoomBotCommands,BotCommandContent}.ts`,
`apps/web/src/autocomplete/CommandProvider.tsx`, `apps/web/src/components/views/rooms/SendMessageComposer.tsx`,
`apps/web/src/slash-commands/status.ts`.

---

## 1. Goal and acceptance criteria

Hermes publishes one MSC4391 command-description state event per dispatchable gateway command in each
eligible Matrix room. Element then lists Hermes commands in `/` autocomplete and sends them in a form
the existing Hermes inbound path executes. When a name collides with an enabled Element built-in, the
user writes it qualified: `/status@hermes`. With Labs "user status" on, Element's own `/status` wins
unqualified.

| # | Acceptance criterion (measurable) |
|---|---|
| A1 | **Coverage.** In every eligible room, the published set equals the gateway command set. It contains every gateway-available `COMMAND_REGISTRY` entry, every plugin command (`get_plugin_commands()`) and every Matrix-eligible skill command (`_iter_gateway_skills("matrix")`), capped at 500. Precedence is core > plugin > skill. Names are lowercase, match `^[a-z0-9][a-z0-9_-]{0,63}$` and dispatch unchanged. Invalid or over-long names are omitted, never truncated. |
| A2 | **Schema.** Every published content is accepted (non-null) by Element's real `parseBotCommandDescription` at the pinned revision. Content is `command` (single token), optional `aliases` (≤10 valid tokens), `description: {"m.text":[{"body": ≤200 chars}]}`, and `parameters` (always an array, ≤32 entries). Each parameter has a unique key matching `^[A-Za-z_][A-Za-z0-9_]{0,63}$`, schema `{"schema_type":"primitive","type":"string"}` and a boolean `optional`. A name or alias never appears in two descriptions from the same bot. |
| A3 | **State key.** `state_key = base64.standard_b64encode(sha256((command + bot_mxid).encode("utf-8")).digest()).decode()`: 44 characters, standard alphabet, `=` padding. Event type is `org.matrix.msc4391.command_description`. The key is percent-encoded in the PUT URL; it may contain `/` and `+`. |
| A4 | **Where.** Hermes publishes only in joined rooms that pass the existing Hermes room gate, `_is_allowed_matrix_room_event`. With `allowed_rooms` empty, that is every room. With an allowlist, it is the listed rooms plus DMs. It also requires `m.room.power_levels` to let the bot send this state type (spec defaults; see §2.4). In a room where the bot lacks power, Hermes sends nothing and logs one INFO line per room per process. Previously published descriptions in a room that is no longer eligible are blanked whenever the bot still has power there. |
| A5 | **When.** Hermes reconciles on these triggers: (a) after the initial sync of every `connect()`, including reconnects; (b) a newly joined room seen in sync; (c) a change of `m.room.power_levels` in a joined room; (d) `/reload-skills`, through the existing adapter seam `refresh_skill_group`. The request's proposed `/reload-mcp` trigger and skill-directory polling are omitted, with reasons in §2.7. |
| A6 | **Diff.** For each room, Hermes reads the room state once. It PUTs only keys whose current content differs from the desired content, or whose event was sent by somebody else. It blanks every non-empty description the bot sent that is no longer desired, with `{}`. A second reconcile with no changes issues zero PUTs. |
| A7 | **Pacing and retries.** Consecutive PUTs are at least 0.2 s apart (≤5 events/s, one worker for all rooms). `M_LIMIT_EXCEEDED` (HTTP 429) retries the same event with backoff: start at 1 s, double each time, cap at 30 s, at most 6 attempts. `M_FORBIDDEN` stops the room and logs one INFO line. Other errors stop that room for this pass with one WARNING. Reconcile runs on its own task: message handling and `/reload-skills` never await it. |
| A8 | **Config, default on.** `matrix.command_descriptions` (YAML bridge row, so `platforms.matrix.extra.command_descriptions` and the bridged twin `MATRIX_COMMAND_DESCRIPTIONS` also work) defaults to true. When false, the next reconcile blanks every non-empty description the bot sent and publishes nothing. |
| A9 | **Execution.** Element publishes the canonical `status`. When typed as `/status@hermes` (qualifier required because Element's `/status` is enabled), Element sends body `/status`, `m.mentions.user_ids=[bot]` and `org.matrix.msc4391.command`. Hermes replies with its gateway status ("📊 **Hermes Gateway Status**") and makes no model request. Inbound code is unchanged. |
| A10 | **Access.** Descriptions are room-wide. Execution keeps every existing check: a sender outside `allowed_users` gets no reply and no turn. |
| A11 | **Conventions.** `plugins/platforms/matrix/adapter.py` is net-shorter than base (FILE_LINES ratchet). New behaviour lives in siblings, with no core (`gateway/`, `hermes_cli/`) changes. The profile scope is inherited from `connect()`. No content is logged. The full required inventory (§4) passes, or failures are proven pre-existing on base. |
| A12 | **Real proof.** Real Synapse, the current-worktree gateway and pinned Element `d698eb22…` in a real browser show: Hermes autocomplete rows including `/status@hermes`; successful `/status@hermes` execution; the power-level and allowlist boundaries; a reload adding and blanking a skill; and the disable toggle blanking everything. Element's own parser accepts the actual published state dumps. Synthetic fixtures alone do not count. |

## 2. Design

### 2.1 What the actual consumer requires (verified in Element `d698eb22`)

| Element behaviour | Location | Hermes consequence |
|---|---|---|
| `command` tokens `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`, ≤3 tokens; `aliases` ≤10 valid tokens; `parameters` **must** be an array ≤32; key `^[A-Za-z_][A-Za-z0-9_]{0,63}$`, unique; schema must be valid; `optional` boolean if present. Any violation drops the whole description. | `BotCommandProtocol.ts:111-150` | Validate every content before publishing; normalise parameter keys (`<model-name>` → `model_name`). |
| A description is shown only if its sender is a *joined* member and is not the local user. The list is deduped per (sender, command) by newest ts and capped at 500 per bot. | `RoomBotCommands.ts:47-90` | Cap at 500 in precedence order. Joined membership is implied: we publish only in joined rooms. |
| `requiresQualifier` is true when a name or alias collides with an *enabled* built-in, or with another bot's command. The qualifier is the sender **localpart** (or full mxid without `@` when localparts collide). | `RoomBotCommands.ts:83-89,105` | `/status@hermes` resolves only for a bot whose localpart is `hermes`. The E2E bot is `@hermes:localhost`. |
| Built-in `/status` is enabled only with `feature_user_status`. | `status.ts:22` | The E2E forces `features.feature_user_status: true` so the collision is real. |
| A name matched by two descriptions from the *same* bot is ambiguous and resolves to nothing. | `RoomBotCommands.ts:116-120` | A name or alias must be unique across the whole manifest. |
| The sent body is `/${command}` + args. The **qualifier is removed**. `m.mentions` names the bot, and a structured block is added when arguments map cleanly. | `BotCommandContent.ts:97-112`, `SendMessageComposer.tsx:436` | Hermes receives `/status`. No inbound change is needed (§2.6). |
| Autocomplete inserts `/${command}@${qualifier}` when qualification is required. | `CommandProvider.tsx:127` | The browser journey asserts the row title `/status@hermes`. |
| A blanked event (`{}`) has no `command`, so it parses to null and is ignored. | `BotCommandProtocol.ts:115-116` | Removal is done by blanking with `{}`. |

MSC4391 text: the state key is "a padded base64 encoded SHA256 digest of the `command` property … AND the mxid
of the sender". The example has the form `sha256(command + mxid)` and uses the standard alphabet. Unstable identifiers:
`org.matrix.msc4391.command_description` and `org.matrix.msc4391.command`.

### 2.2 Module boundaries (all inside the Matrix plugin; core untouched)

```
plugins/platforms/matrix/
├── adapter.py             facade: −40 lines (bang helpers moved out) + 5 hook lines → net shorter
├── adapter_commands.py    NEW  Matrix slash-command surface (pure, sync):
│                               moved `!command` normalisation + MSC4391 manifest builder
└── command_publisher.py   NEW  async reconcile worker, power-level evaluation, diff planning,
                                mautrix state API wrapper, MatrixCommandDescriptionsMixin
```

**Ruling: Keep the derivation plugin-local instead of adding a `matrix_*` function to
`hermes_cli/commands_platforms.py`.** Plugins never touch core, and every needed input is already
importable. Cost if wrong: a future refactor of those private helpers breaks the Matrix import and
fails loudly in unit tests.

#### `adapter_commands.py` (pure; call through `asyncio.to_thread`: it reads config and scans skills)

```python
COMMAND_DESCRIPTION_EVENT_TYPE = "org.matrix.msc4391.command_description"
MAX_COMMANDS_PER_BOT = 500
DESCRIPTION_LIMIT = 200

# moved verbatim from adapter.py (names unchanged; three tests update their import path)
_MATRIX_BANG_COMMAND_RE: re.Pattern[str]
def _resolve_matrix_bang_command(name: str) -> str | None
def _normalize_matrix_bang_command(text: str) -> str

def command_state_key(command: str, bot_user_id: str) -> str
def command_parameters(args_hint: str, *, subcommands: Sequence[str] = (),
                       default_args: bool = False) -> list[dict[str, Any]]
def description_content(command: str, description: str, parameters: list[dict[str, Any]],
                        aliases: Sequence[str] = ()) -> dict[str, Any]
def is_valid_description(content: Mapping[str, Any]) -> bool    # mirror of Element parse() limits
def build_command_manifest(bot_user_id: str) -> dict[str, dict[str, Any]]
    # state_key -> content, insertion order = precedence; never raises for one bad entry
```

The manifest rules are as follows:

- **Sources and order.**
  1. Core: `hermes_cli.commands_platforms._gateway_available_commands()`, in registry order. The name is `cmd.name`, the description `cmd.describe()`, the hint `cmd.args_hint`, and `cmd.subcommands` is passed to the parameter rule.
  2. Plugins: `hermes_cli.plugins.get_plugin_commands()`, sorted by name. The description is `meta["description"]` or `t("slash.shared.plugin_command_desc")`, and the hint is `meta.get("args_hint", "")`.
  3. Skills: `hermes_cli.commands_platforms._iter_gateway_skills("matrix")`, alphabetical. The name is `cmd_key.lstrip("/")`; the description is `info["description"]`.

  An exception from one source is logged with a traceback and that source is skipped. The other sources still publish.
- **Names.** Names are lowercased and published only if they match `^[a-z0-9][a-z0-9_-]{0,63}$`; otherwise they are omitted with a DEBUG log. They are never transformed or clamped. **Ruling:** reject the proposal's `_clamp_command_names` (32-character truncation). A truncated name does not dispatch, and Element allows 64 characters. Cost if wrong: names of 65 characters or more stay unpublished, and they are still typeable.
- **Collisions.** The reserved set starts with all core canonical names, then valid core aliases (each command keeps at most 10). Plugin and skill names already taken are dropped. Aliases are added only if not yet taken, so no name resolves twice.
- **Cap.** The first 500 entries are kept; one INFO line reports how many were hidden.
- **Parameters** (`command_parameters`):
  1. An empty hint gives `[]`. Exception: skills and plugins with an empty hint, which are called with `default_args=True`, get one `{"key":"args","optional":true}` parameter.
  2. A hint that is exactly one placeholder (`<x>` or `[x]`, where `x` matches `[A-Za-z][A-Za-z0-9 _-]{0,62}`, contains no `|`, and is not one of `subcommands`) gives one string parameter. Its key is `x` lowercased, with `[^a-z0-9_]+` replaced by `_` and edge `_` stripped. It is optional iff the bracket is `[`, and has no description.
  3. Any other hint gives one string parameter `args`, required iff the hint starts with `<`. Its description is `{"m.text":[{"body": hint[:200]}]}`.

  Example: `/status` (`[status]`, subcommand `status`) gets one optional `args` described `[status]`.
- **Descriptions.** Whitespace is collapsed and the text is cut to 200 characters with a `...` tail, as `{"m.text":[{"body":…}]}`. `aliases` is omitted when empty. `optional` is always present.
- **Validation.** Every content passes `is_valid_description` before inclusion; a failure is a DEBUG log and the entry is skipped. Tests prove the builder never emits an invalid entry.

#### `command_publisher.py`

```python
class StateApi(Protocol):
    async def get_room_state(self, room_id: str) -> list[dict[str, Any]]: ...
    async def put_room_state(self, room_id: str, event_type: str, state_key: str,
                             content: dict[str, Any]) -> None: ...

class MautrixStateApi:   # client.api.request(Method.GET/PUT, Path.v3.rooms[r].state[t][k], ...)
    def __init__(self, client: Any) -> None
    # PathBuilder percent-encodes every segment (safe=""), so '/', '+', '=' in keys are safe.

def power_level_allows(state: Sequence[Mapping[str, Any]], user_id: str,
                       event_type: str) -> tuple[bool, int, int]      # (allowed, user_level, required)
def plan_room(state: Sequence[Mapping[str, Any]], bot_user_id: str,
              desired: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, dict[str, Any]]]
    # ordered (state_key, content): changed/new desired entries in manifest order, then {} blanks

class MatrixCommandPublisher:
    def __init__(self, *, api: StateApi, bot_user_id: str, enabled: bool,
                 is_room_eligible: Callable[[str], Awaitable[bool]],
                 build_manifest: Callable[[], dict[str, dict[str, Any]]],
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 interval: float = 0.2) -> None
    def start(self) -> None                # creates the worker task, then request_full()
    async def stop(self) -> None           # cancel + await worker; idempotent
    def request_full(self) -> None         # rebuild manifest, then every known room
    def request_rooms(self, room_ids: Iterable[str]) -> None
    def observe_sync(self, sync_data: Mapping[str, Any]) -> None
        # new rooms.join ids -> request_rooms; m.room.power_levels in state/timeline -> request_rooms;
        # rooms.leave -> forget (and drop from queue). Pure bookkeeping; never awaits.

class MatrixCommandDescriptionsMixin:     # first base of MatrixAdapter
    _command_publisher: MatrixCommandPublisher | None = None    # object.__new__ test doubles
    def _start_command_descriptions(self) -> None     # builds publisher from self._client/config
    async def _stop_command_descriptions(self) -> None
    def _observe_command_description_sync(self, sync_data: dict[str, Any]) -> None
    async def refresh_skill_group(self) -> None       # existing /reload-skills seam -> request_full()
```

`power_level_allows` uses spec defaults:
- **No `m.room.power_levels`:** required level 0, so sending is allowed.
- **Otherwise:** required is `events[type]`, falling back to `state_default` and then 50. The user's level is `users[user]`, falling back to `users_default` and then 0. String values are coerced with `int()` and invalid ones treated as missing. Sending is allowed iff user level ≥ required.
- **Room version `12` creators** (the `m.room.create` sender or a member of `additional_creators`) are always allowed.

### 2.3 Data flow and adapter wiring (5 hook lines in `adapter.py`)

```
connect(): initial sync ──► _absorb_sync ──► _observe_command_description_sync(sync_data)   (records rooms)
           _mark_connected() ──► _start_command_descriptions() ──► publisher.start() ──► request_full()
_sync_loop: _absorb_sync ──► observe_sync  (new joins, power-level changes, leaves)
/reload-skills (gateway/slash_commands.py:1134 loop) ──► adapter.refresh_skill_group() ──► request_full()
disconnect(): await _stop_command_descriptions()   (before closing the HTTP session)

worker loop (one task, created inside connect() → inherits the profile ContextVars):
  full requested? manifest = await asyncio.to_thread(build_manifest)   (enabled=False → {})
  for room in queue (deduped, FIFO):
      desired = manifest if await adapter._is_allowed_matrix_room_event(room) else {}
      state   = await api.get_room_state(room)
      ops     = plan_room(state, bot, desired);  if not ops: continue
      allowed, have, need = power_level_allows(state, bot, EVENT_TYPE)
      if not allowed: INFO once per room ("cannot publish … needs %d has %d"); continue
      for key, content in ops: paced PUT with 429 backoff
      INFO "Matrix: command descriptions in %s: %d published, %d cleared"
```

Adapter edits:
- `class MatrixAdapter(MatrixCommandDescriptionsMixin, BasePlatformAdapter)`.
- Import the mixin and `_normalize_matrix_bang_command` from the siblings.
- One call each in `connect()` (after `_mark_connected()`), `disconnect()` (with the other task cancellation), and `_absorb_sync` (after `rooms_join` bookkeeping).
- Add one `_YAML_BRIDGE` row: `("command_descriptions", "MATRIX_COMMAND_DESCRIPTIONS", "lower")`.

Hook bodies tolerate a missing or None publisher. `enabled` is read in `_start_command_descriptions` via the existing `self._extra_truthy(self.config, "command_descriptions", "MATRIX_COMMAND_DESCRIPTIONS", "true")`. That reader is scope-aware through `extra_or_secret`.

**Profile scope:** the worker task is created inside `connect()`, which already runs in the owning profile's scope, and `asyncio.to_thread` copies context. So skill, plugin and config reads resolve the adapter owner's home, including when `/reload-skills` fires from another context.

**Known limitation:** a shared bot that routes rooms to satellite profiles advertises the owner profile's command set, the same as Telegram's and Discord's menus. This is recorded as a ruling.

### 2.4 Error handling

| Condition | Behaviour |
|---|---|
| Manifest build raises | `logger.exception`; keep the previous manifest (or skip the pass if there is none). The worker keeps running. |
| `get_room_state` fails (`MatrixRequestError`, `aiohttp.ClientError`, `TimeoutError`) | WARNING once per room per pass; skip the room. The next trigger retries. |
| `MLimitExceeded` | Wait `delay`, retry the same PUT. `delay` starts at 1 s, doubles, is capped at 30 s, and persists across events in the pass (so it converges under sustained limits). After 6 attempts: WARNING, skip the rest of the room. |
| `MForbidden` on PUT | INFO once per room; skip the room; mark it so the PL-change trigger retries. |
| Any other request error on PUT | WARNING; skip the rest of the room. |
| `CancelledError` | Propagates (disconnect). |
| Unexpected exception in the loop body | `logger.exception`; continue with the next room. BLE001 requires the traceback. |

Logs carry room IDs, counts and power levels only — never content.

### 2.5 Configuration (reconciled with current conventions)

- **Canonical setting.** `matrix: { command_descriptions: false }` in `config.yaml`, the documented top-level `matrix:` block. `platforms.matrix.extra.command_descriptions` also works.
- **Env twin.** `MATRIX_COMMAND_DESCRIPTIONS` exists only because `_YAML_BRIDGE` bridges every `matrix:` key, like `auto_thread`. It is not a `HERMES_*` variable (so HX002 doesn't apply), and docs present YAML as the way to configure.
- **Reading.** Readers use the existing extra-first, scope-aware `extra_or_secret`, so multiplexed profiles never borrow the launch profile's value.
- **Ruling: Use the bridged YAML key with its env twin, rather than an extra-only key.** This matches every other Matrix behaviour key; without a bridge row, a `matrix:` YAML key never reaches `extra`. Cost if wrong: one extra env name to document.
- **Default.** True, as the request requires. This is a new feature, not a changed default, so no migration guard is needed.

### 2.6 Inbound execution (verified; no change)

Element sends `{"msgtype":"m.text","body":"/status","m.mentions":{"user_ids":["@hermes:…"]},"org.matrix.msc4391.command":{"command":"status","arguments":{}}}`. Hermes handles it as follows:
1. `_on_room_message` → `_handle_text_message`.
2. `_resolve_message_context`: `/` bodies and `m.mentions` both pass `require_mention`.
3. `_build_inbound_event` marks the message `MessageType.COMMAND`.
4. `MessageEvent.get_command()` gives `status` (it also strips any `@qualifier` a user might type manually).

**Ruling:** no inbound hardening. Element always sends a body consistent with the block, and qualifier stripping already exists. Cost if wrong: a non-Element client sending only the block would need a follow-up.

### 2.7 Rulings on the old §2.7 proposal

| Proposal item | Decision |
|---|---|
| `/reload-mcp` trigger | **Dropped.** MCP servers contribute tools, never slash commands (no MCP code path in `hermes_cli/commands.py`, `agent/skill_commands.py` or the plugins command registry). |
| Skill-directory signature watch | **No polling.** Every reconcile rebuilds the manifest through `get_skill_commands()`, which re-checks the signature. On-disk edits appear on `/reload-skills`, reconnect or restart, as for Telegram/Discord. Cost if wrong: an edited skill stays stale until reload. |
| Power-level change | **Added** as a trigger, so granting power publishes without a restart. |
| `_clamp_command_names` 32-char | **Replaced** by omitting invalid names and allowing 64 characters (§2.2). |
| "DMs + `MATRIX_ALLOWED_ROOMS` rooms" | **Implemented as the actual gate** `_is_allowed_matrix_room_event`; an empty allowlist means all joined rooms, matching where Hermes answers. |
| `MATRIX_COMMAND_DESCRIPTIONS` env | Kept as the YAML-bridge twin (§2.5). |
| Optional inbound hardening | Not needed (§2.6). |

### 2.8 Other conventions

- **Footprint.** No new core tool, hook, `HERMES_*` variable or i18n string; existing strings or plain hint text are reused.
- **Code health.** New functions keep CC ≤20, ≤300 lines and nesting ≤6.
- **Async.** `asyncio.to_thread` for config and skill I/O (HX007), and held task references (RUF006).
- **Docs.** `website/docs/user-guide/messaging/matrix.md` gets a "Command descriptions (Element autocomplete)" section. It covers what is published, where, qualification, power levels, the toggle, and Synapse `rc_message`: the default 0.2/s takes about 12 min per room for ~150 commands, so recommend a bot ratelimit override. `website/docs/reference/environment-variables.md` lists the bridged twin.

## 3. Tasks

The dependency graph is P0 → {T1, T2} → T3 (T2 needs only P0). There is no early review: the
wire contract is pinned to Element source. Real-consumer validation runs in the E2E phase. The consolidated
feature review covers T1–T3 together.

### P0 — environment (orchestrator, before any gate; shared build resource)

```bash
set -euo pipefail
cd /home/sinkerine/.paseo/worktrees/15zkmyhp/matrix-command-descriptions
export MCD_PM="$PWD/.hermes-sandbox/pm"
export HERMES_HOME="$MCD_PM/home" HERMES_RUNTIME_DIR="$MCD_PM/home/tools"
export XDG_CACHE_HOME="$MCD_PM/cache" UV_CACHE_DIR="$MCD_PM/cache/uv" UV_PYTHON_INSTALL_DIR="$MCD_PM/python"
umask 077; mkdir -p "$HERMES_HOME"; test ! -e .venv
python3 -m pm.build_env --source . --out "$PWD/.venv" --group dev --group test --extra matrix
.venv/bin/python -c "import pytest, mautrix, olm, aiosqlite, asyncpg; from mautrix.crypto import OlmMachine"
```

- **Interpreter.** System `/run/current-system/sw/bin/python3` (3.14.8, satisfies `>=3.14,<3.15`) runs `pm.build_env` (verified with `--help`).
- **External dependency.** Network access for PM/uv wheels, plus `python-olm` native build tools.
- **Fallback** if the system Python is refused: the same env, then `scripts/run-in-hermes-env python -m pm.build_env …`.
- **On failure** report BLOCKED. Never pip/uv into it, never reuse another worktree's environment, never touch `~/.hermes`.
- **Isolation.** Every unit/E2E command below sets `HERMES_PYTHON="$PWD/.venv/bin/python"` and keeps `HERMES_HOME`/`HERMES_RUNTIME_DIR` pointed at `$MCD_PM`, so the runner never adopts the account tool store. Nobody rebuilds `.venv` while tasks run.

### T1 — Publisher (manifest, reconcile worker, adapter wiring, docs) · wave 1 · **subtle (high thinking)**

- **Goal.** Everything in §2.2–§2.8, plus unit tests.
- **Owned files.**
  - Plugin code: `plugins/platforms/matrix/adapter.py`, `plugins/platforms/matrix/adapter_commands.py` (new), `plugins/platforms/matrix/command_publisher.py` (new).
  - New tests: `tests/plugins/platforms/matrix/test_command_manifest.py`, `tests/plugins/platforms/matrix/test_command_publisher.py`.
  - Import-path-only updates for the moved bang helper: `tests/gateway/test_matrix.py`, `tests/gateway/test_project_command.py`, `tests/gateway/test_project_memory_command.py`.
  - Docs: `website/docs/user-guide/messaging/matrix.md`, `website/docs/reference/environment-variables.md`.
- **Consumes.** The registry, plugin and skill helpers named in §2.2; `_is_allowed_matrix_room_event`, `_extra_truthy` and `_YAML_BRIDGE`; mautrix `Method`/`Path`/`MLimitExceeded`/`MForbidden`/`MatrixRequestError`.
- **Produces.** The §2.2 interfaces exactly, plus the wire contract in A2/A3 and the config key in §2.5.
- **Tests.** Behaviour contracts, no change-detectors, no sleeps; inject `sleep`.
  - Manifest from real discovery on a temp `HERMES_HOME`, with one SKILL.md and one plugin registering a command through `ctx.register_command`:
    - `status` is present, its content follows the §2.2 parameter rule, and `parameters` is an array;
    - every entry passes a Python mirror of Element's `parse()`;
    - core beats a same-named skill;
    - invalid and 65-character names are omitted;
    - no name or alias is duplicated, and the cap is ≤500;
    - the state key equals an independently computed padded standard base64.
  - `power_level_allows` table: no PL event, `events` override, `state_default`, `users_default`, string values, v12 creator.
  - `plan_room`: unchanged → no ops; changed; foreign sender on our key; stale own blanked; own blank ignored; disabled → blank all own; ineligible → blank.
  - Worker behaviour:
    - pacing ≥ interval;
    - 429 then success, with the backoff sequence;
    - 6× 429 abandons the room;
    - 403 logs once and skips;
    - a GET failure skips the room;
    - full-while-running coalesces;
    - `observe_sync` reacts to joins, PL changes and leaves;
    - `refresh_skill_group` returns without awaiting the PUTs;
    - `stop()` cancels.
  - Adapter: the mixin hooks are no-ops on `object.__new__` instances; `adapter.py` line count is below base (asserted by the code-health gate, not by a source-reading test).
- **Dependencies.** P0.
- **Unit command.**
  ```bash
  HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/plugins/platforms/matrix/test_command_manifest.py tests/plugins/platforms/matrix/test_command_publisher.py tests/gateway/test_matrix.py tests/gateway/test_project_command.py tests/gateway/test_project_memory_command.py tests/gateway/test_reload_skills_command.py tests/gateway/test_shared_platform_boilerplate.py -v && "$PWD/.venv/bin/python" -m scripts.code_health --base db722732ad1e7002cd06fc3c813a109626466d59
  ```

### T2 — Real Synapse + pinned Element browser harness · wave 1 (parallel with T1)

- **Goal.** A self-contained disposable harness for E2E-B (§5.2).
- **Owned files.** `scripts/sandbox/matrix_command_descriptions/**`, plus one discoverability sentence in `tests/AGENTS.md` (chain stays well under 30k).
- **Files in the harness.**
  - `README.md`: recipe, boundaries, and the "E2E when first added only; unit tests always" rule.
  - `env.sh`: asserts the worktree root and `SB=$PWD/.hermes-sandbox/matrix-commands`. Sets `HERMES_HOME=$SB/home`, `HERMES_RUNTIME_DIR=$SB/home/tools` and `HERMES_PYTHON=$PWD/.venv/bin/python` (must exist). Redirects every cache and TMP into `$SB` and puts `$SB/bin` first on PATH. Does not change `HOME` and does no provisioning.
  - `common.py`: paths and private 0600 I/O.
  - `procs.py`: owned process groups with pid+starttime receipts. These are new; the UI harness's are bound to its own SB.
  - `harness.py`: subcommands `preflight`, `build-element`, `verify-element`, `up`, `restart-gateway [--disable]`, `dump-state --room R --label L`, `set-bot-power --room R --level N`, `bob-command --room R --text T`, `skill add|remove`, `browser --step S`, `conformance`, `selftest`, `down [--purge]`.
  - `provider_stub.py`: loopback OpenAI-compatible stub that answers `/v1/models` and counts `/v1/chat/completions` (expected 0).
  - `browser_journey.mjs`: Playwright steps.
  - `element/HermesPublication.test.ts`: conformance spec, copied into the sandbox Element tree only.
- **Pins and resources.**

  | Item | Value |
  |---|---|
  | Element commit | `d698eb223e7dc3030fed2c49330e9ecb98b4ab0d`, from `git archive` of the read-only checkout into `$SB/element-src` |
  | pnpm | 12.11.2 via `COREPACK_HOME=$SB/cache/corepack corepack install -g pnpm@12.11.2`, shimmed into `$SB/bin` |
  | Element build | `pnpm install --frozen-lockfile --ignore-scripts`, then `pnpm --filter element-web rebuild @element-hq/element-call-component matrix-js-sdk`, then `pnpm --config.verifyDepsBeforeRun=false --filter element-web run build`; the same proven recipe as the UI harness |
  | Provenance | Commit, lock sha256 and webapp tree sha256 written to `evidence/element-build-provenance.json`; `verify-element` recomputes it |
  | Synapse | `matrixdotorg/synapse@sha256:1909bcc07d4180945464712a7f4d4bb6f644c4c1774d026d77cdadc196b91eb8` (present locally), loopback ports 18108 (Synapse) / 18181 (Element) / 18182 (stub) |
  | Container | Name `hermes-mcd-<owner16>`, label `org.hermes.matrix-command-sandbox=<owner16>` |
  | Synapse config | `rc_message: {per_second: 4, burst_count: 30}`, so the 5/s pacing provokes real 429s |
  | Accounts | `@hermes:localhost` (bot, E2EE required, stable device), `@alice:localhost` (allowed), `@bob:localhost` (not allowed) |
  | Element config | `features.feature_user_status: true`, sandbox homeserver |
  | Browser | Playwright from `$SB/element-src/node_modules/.pnpm/playwright@*/`, driving `/nix/store/dqzn64123p2qvidnx0wspr0z96rc407a-google-chrome-156.0.8078.79/bin/google-chrome-stable`; persistent contexts under `$SB/browser/<user>`, `cwd=$SB/tmp`, relative TMPDIR, `chromiumSandbox: true` |
- **Ruling: Write a new harness instead of extending `matrix_interactive_ui`.** The UI harness hard-pins its interpreter (the preflight failure), its Element commit, its `hermes-bot` account (which breaks the `@hermes` qualifier), its `interactive_ui` toolset and its SB-bound process receipts. Changing them risks that feature's recorded receipts. Cost if wrong: about 100 duplicated lines of process ownership.
- **Consumes.** The A2/A3 wire contract, the config key from §2.5, and `python -m gateway.run`.
- **Produces.** The commands above; T2 does not depend on T1 code.
- **Dependencies.** P0 (for `.venv`).
- **Unit command** (offline only; starts no services):
  ```bash
  bash -n scripts/sandbox/matrix_command_descriptions/*.sh && "$PWD/.venv/bin/python" -I -m py_compile scripts/sandbox/matrix_command_descriptions/*.py && (source scripts/sandbox/matrix_command_descriptions/env.sh && python scripts/sandbox/matrix_command_descriptions/harness.py selftest) && "$PWD/.venv/bin/python" scripts/check-windows-footguns.py scripts/sandbox/matrix_command_descriptions && node --check scripts/sandbox/matrix_command_descriptions/browser_journey.mjs
  ```
  `selftest` covers the generated gateway/Element/Synapse configs, the state-key and assertion helpers on fixture dumps, refusal to adopt unowned state, receipt identity checks, and secret redaction.

### T3 — Matrix stand-in state API + sandbox feature E2E journeys · wave 2 · after T1

- **Goal.** Repo-recipe E2E: a real `hermes gateway run` against the local stand-in (§5.1).
- **Owned files.** `tests/fakes/platforms/matrix_standin.py` and `tests/e2e/core/platforms/test_matrix_standin_smoke.py` (honesty coverage for the new routes). Two new journey files:
  - `tests/e2e/core/platforms/test_matrix_command_descriptions.py` (publication, execution, access, reload, power level);
  - `tests/e2e/core/platforms/test_matrix_command_descriptions_cleanup.py` (restart diff, stale and disallowed blanking, disable).

  These two files are split so each stays under the runner's 300 s per-file timeout. `_drv_matrix.py` is unchanged; journeys add their own rooms.
- **Stand-in additions.**
  - `GET /v3/rooms/{room_id}/state` returns the full list.
  - `PUT /v3/rooms/{room_id}/state/{event_type}/{state_key:.*}` enforces spec power levels (`M_FORBIDDEN` 403), replaces the state and appends it to the timeline so sync delivers it, and returns `event_id`.
  - New helpers: `set_power_levels(room_id, users, state_default=50)`, `post_content(room_id, sender, content)` and `state_events(room_id, event_type)`.
  - The existing `take_fault` supports `M_LIMIT_EXCEEDED` 429.
- **Consumes.** The T1 behaviour, `GatewayUnderTest`, `MatrixDriver`, `FakeLLMServer`/`Director`.
- **Dependencies.** T1 implemented, with its gate passed and its interface matching. The files are disjoint from T2.
- **Unit command.**
  ```bash
  HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/e2e/core/platforms/test_matrix_standin_smoke.py tests/e2e/core/platforms/test_matrix_command_descriptions.py tests/e2e/core/platforms/test_matrix_command_descriptions_cleanup.py -v
  ```
  The journeys must PASS, not skip.

### External and independent work

| Dependency | Status |
|---|---|
| Network for P0, pnpm, Element dependencies and the Docusaurus build | Unverified until run. The only external dependency besides the local Docker daemon (present). |
| Synapse image | Present |
| Element source at the pin | Present |
| Nix Chrome | Present |
| Paseo browser tools | Not required |

No external producer is needed: Element is read-only and already contains the consumer.

## 4. Test and verification

### 4.1 Environment, runner, discovery

| Aspect | Detail |
|---|---|
| Runner | `scripts/run_tests.sh` only, with explicit `HERMES_PYTHON="$PWD/.venv/bin/python"` (P0). The runner gives per-file subprocess isolation, a temp `HERMES_HOME` per test, `TZ=UTC` and no credentials. Discovery is by the file and directory arguments shown. |
| Matrix-extra imports | Must execute, not skip. Check the summary for `importorskip` skips; any skip in C1–C4 fails the check. |
| Generated inputs | None committed. Element source, builds and caches are generated under `$SB`. `pnpm install` and builds write only `$SB/element-src` and `$SB/cache`. The Docusaurus build writes `website/node_modules` and `website/build` (gitignored). |
| Commands that rewrite files | None among the checks. Do not run formatters or generators. |
| Profile-archive guard | It walks the filesystem and flags `.tgz` caches under `.hermes-sandbox/`, so the final lint judges the committed tree snapshot (C7). |

### 4.2 Required inventory

| # | Check | Command (from the worktree, P0 env exported) | Group |
|---|---|---|---|
| C1 | T1 unit and adjacent regressions | T1 unit command | unit |
| C2 | Matrix plugin suite | `HERMES_PYTHON=… scripts/run_tests.sh tests/plugins/platforms/matrix/` | unit |
| C3 | Matrix gateway regressions | `HERMES_PYTHON=… scripts/run_tests.sh tests/gateway/test_matrix*.py tests/gateway/test_project_command.py tests/gateway/test_project_memory_command.py tests/gateway/test_reload_skills_command.py tests/gateway/test_reload_skills_discord_resync.py tests/gateway/test_shared_platform_boilerplate.py tests/gateway/test_multiplex_transport_matrix.py tests/gateway/test_native_producer_owner_matrix.py tests/gateway/test_suppression_contract_matrix.py` | unit |
| C4 | All platform E2E journeys, including the new ones and the existing Matrix routing/memory/smoke journeys against the extended stand-in | `HERMES_PYTHON=… scripts/run_tests.sh tests/e2e/core/platforms/ -v` | e2e-standin |
| C5 | Harness offline | T2 unit command | lint |
| C6 | Full suite | `HERMES_PYTHON=… scripts/run_tests.sh`. For each failing file, re-run the same file against a base snapshot (`git archive db722732ad… \| tar -x -C .hermes-sandbox/base-src`, same interpreter) and record whether it is pre-existing. A failure new on HEAD blocks. | full |
| C7 | Lint: ruff, code-health ratchet (adapter FILE_LINES ≤ base), HX rules, guards | Development: `"$PWD/.venv/bin/python" scripts/check --only ruff` and `"$PWD/.venv/bin/python" -m scripts.code_health --base db722732ad1e7002cd06fc3c813a109626466d59`. Final, after commits: `"$PWD/.venv/bin/python" scripts/check --commit HEAD --base db722732ad1e7002cd06fc3c813a109626466d59`. Any pre-existing finding, such as the root AGENTS.md size, is compared against base and recorded, never waived silently. | lint |
| C8 | Docs | `"$PWD/.venv/bin/python" website/scripts/check_doc_links.py`; `HERMES_PYTHON=… scripts/run_tests.sh tests/website/`; `cd website && npm_config_cache="$PWD/../.hermes-sandbox/pm/cache/npm" npm ci && npm run lint:diagrams && npm run build:fast` (network) | docs |
| C9 | Pinned consumer's own suite on the built artifact | In `$SB/element-src` (via `env.sh`): `pnpm -w exec vitest run apps/web/src/slash-commands/bot-commands/` | browser |
| C10 | Element parser on real publication | `harness.py conformance`. It copies `element/HermesPublication.test.ts` into the sandbox Element tree and runs `pnpm -w exec vitest run apps/web/src/slash-commands/bot-commands/HermesPublication.test.ts` over the actual `dump-state` JSON (E2E-B steps B3, B6, B9, B11). | browser |
| C11 | E2E-B browser journey (§5.2) | Harness steps | browser |

There is no Python type checker in `scripts/check` or CI for these paths. Element type and lint checks are
not ours to run on an unmodified pinned artifact; C9 proves consumer compatibility. Desktop E2E is excluded
by instruction. No visual baseline exists for Hermes; screenshots are evidence, not baselines.

### 4.3 Resource groups and concurrency (limit 2)

- **unit.** Read-only `.venv` and temp homes; may overlap with any group.
- **e2e-standin.** Ephemeral loopback ports and its own gateway process groups; may overlap with unit and browser. Never two runs of the same journey file at once.
- **browser.** Docker container and fixed ports 18108/18181/18182 in `$SB`; serialized with itself.
- **full.** CPU-heavy; run alone or with browser only.
- **docs.** `website/node_modules`; run alone.
- **Build lock.** Default `<git dir>/e2e-feature-build.lock` for anything else. `.venv` is never rebuilt during checks.

Reusable equivalence: C1 ⊂ C3 and C2, so a C2+C3 receipt on identical inputs satisfies C1. C4 covers T3's gate.

## 5. E2E journeys

E2E runs when the feature is first added. Future pushes and deployments need not rerun it, but existing unit tests (C1–C3, C6, C7) still run.

### 5.1 E2E-A — sandbox feature E2E (repo recipe: `tests/AGENTS.md` § Sandbox feature E2E)

The journey uses a real `hermes gateway run` child, the extended Matrix stand-in and `FakeLLMServer`, all in pytest tmp homes.
Call `MatrixDriver.arm_readiness()` before every `start()`. Assertions are on stand-in calls, state and delivered
messages, and on model request counts — not logs.

Journey `test_matrix_command_descriptions.py`. Rooms:
- `alpha`: group, allowlisted, no PL event;
- `beta`: group, allowlisted, PL `state_default` 50 with the bot at 0;
- `gamma`: group, not allowlisted;
- `dm`: two members.

Users: admin (allowed) and `@intruder` (joined, not allowed).

1. Start the gateway. Bot-sent descriptions appear in `alpha` and `dm`:
   - every state key is the padded standard base64 of sha256(command+bot);
   - the `status` content has `parameters` as an array and the §2.2 `args` parameter;
   - every content passes the Element-mirror validator.

   No PUT is ever attempted in `beta` or `gamma`. Consecutive PUT timestamps are ≥0.19 s apart.
2. A one-shot `M_LIMIT_EXCEEDED` fault on a PUT leads to a recorded faulted call, then a successful PUT of the same key.
3. Admin posts the Element-shaped content (§2.6) in `alpha`. The bot reply contains "Hermes Gateway Status" and the model received 0 requests. `@intruder` posts the same: no bot reply within 10 s.
4. Add `skills/mcd-probe/SKILL.md` and send `/reload-skills`. The `mcd-probe` description appears in `alpha` and `dm`, and the reply is not blocked by publication.
5. `set_power_levels(beta, bot=50)` makes `beta` receive the full set, with no restart.

Journey `test_matrix_command_descriptions_cleanup.py`, on the same room layout with everything already published in its first phase:

6. Restart unchanged: zero PUTs (diff efficiency).
7. Restart with `mcd-probe` removed and `alpha` dropped from `allowed_rooms`:
   - `mcd-probe` is blanked `{}` in `dm`;
   - every bot description in `alpha` is blanked `{}`;
   - other keys in `dm` are untouched.
8. Restart with `matrix.command_descriptions: false`: every remaining non-empty bot description in every room becomes `{}`, and nothing new is published.

### 5.2 E2E-B — real Synapse + current-worktree gateway + pinned Element browser

Run from the worktree after `source scripts/sandbox/matrix_command_descriptions/env.sh`. Evidence goes to `$SB/evidence/`
and is mirrored to `<run dir>/e2e/`. `verify-element` runs before and after.

| Step | Action | Expected result and evidence |
|---|---|---|
| B0 | `harness.py preflight`, `build-element`, `verify-element`, `up` | All available. Synapse is ready. Accounts are created. The harness creates and joins rooms via API: `ops` (alice+bob+bot, allowlisted, bot PL 0, unencrypted) and `elsewhere` (alice+bob+bot, not allowlisted, bot PL 100). The gateway is connected. Evidence: `provenance.json`, `gateway.log`. |
| B1 | `browser --step login-and-dm` | Alice logs in and starts a DM with `@hermes:localhost`; the bot joins. The DM is encrypted (room settings). Screenshot `01-dm.png`. |
| B2 | Wait for the gateway INFO summary for the DM | `N published, 0 cleared`, with N ≥ 100. The log shows ≥1 `M_LIMIT_EXCEEDED` retry under `rc_message` 4/s. |
| B3 | `dump-state --room dm --label dm-initial` | Bot-sent `org.matrix.msc4391.command_description` events. Keys are 44-character padded standard base64 equal to recomputed values. `status` is present. |
| B4 | `browser --step autocomplete-status` (type `/sta`) | Rows include Element's built-in `/status` and a Hermes row titled `/status@hermes` with "… (from hermes)". Screenshot `02-autocomplete-status.png`. |
| B5 | `browser --step run-status` (choose the row, Enter) | The bot reply contains "Hermes Gateway Status" and the DM room id. The sent event, read via `window.mxMatrixClientPeg`, has body `/status`, `m.mentions.user_ids=["@hermes:localhost"]` and an `org.matrix.msc4391.command.command` of `status`, saved to `sent-status.json`. The stub counts 0 completions. Screenshot `03-status-reply.png`. Then type `/status` unqualified + Enter: Element's built-in error dialog (needs args), and no message reaches Hermes. Screenshot `04-builtin-status.png`. |
| B6 | `dump-state` for `ops` and `elsewhere`; `browser --step ops-empty` | No Hermes descriptions in either room. One gateway INFO line names `ops` with need 50 / have 0. The `/` autocomplete in `ops` has no Hermes rows. Screenshot `05-ops-no-power.png`. |
| B7 | `set-bot-power --room ops --level 50`, then wait | The PL-change trigger publishes the set in `ops` (`dump-state … ops-granted`). Autocomplete in `ops` now has Hermes rows. Screenshot `06-ops-granted.png`. |
| B8 | `bob-command --room ops --text /status` (Element-shaped content) | No bot reply within 20 s, and 0 stub completions. Evidence: `bob-denied.json`. |
| B9 | `skill add`, then Alice sends `/reload-skills` in the DM via the browser | The reload reply arrives promptly. `mcd-probe` appears in DM state and in autocomplete. Screenshot `07-reload-added.png`. Then `skill remove` and `/reload-skills` again: `mcd-probe` is blanked `{}` (`dump-state … dm-removed`) and disappears from autocomplete. |
| B10 | `harness.py conformance` (C10) over `dm-initial`, `ops-granted` and `dm-removed` | Every bot non-empty event parses non-null. `getRoomBotCommands` (feature_user_status on) marks `status` as `requiresQualifier`. `resolveBotCommand("/status@hermes")` resolves, and `createBotCommandContent` gives body `/status`. Blanked events parse to null. |
| B11 | `restart-gateway --disable`, then wait | Every bot description in the DM and `ops` becomes `{}` (`dump-state … disabled`). Autocomplete has no Hermes rows. `/status@hermes` + Enter gives Element's unknown-command dialog. Screenshot `08-disabled.png`. Conformance over `disabled` shows that all bot events parse to null. |
| B12 | `verify-element`; `down` | The artifact digest is unchanged. Owned services are stopped and evidence retained. `--purge` only on explicit request. |

**Fallback:** if Element ↔ bot E2EE blocks B1/B5, create an unencrypted DM via API and record the limitation. Publication does not depend on encryption.
A remote browser, a missing Nix Chrome, a failed Element install or no network is reported as a blocker, never replaced by fixtures.

## 6. Risks and open notes

- **Real-world Synapse rate limits.** At the default `rc_message` of 0.2/s, the first publication takes about 12 minutes per room. The docs recommend a ratelimit override for the bot.
- **State volume.** Each eligible room carries about 150 state events per bot, by MSC4391's design.
- **Routed satellite profiles** advertise the owner profile's command set (§2.3).
- **Network-dependent setup** (P0, pnpm, Element dependencies, Docusaurus) is unverified until run.
