# Project ↔ Hindsight banks — implementation plan

Status: **approved by the user (2026-11-06)** · Base: `origin/main` @ `422eced44d`
· Branch: `e2e-feature-matrix-room-hindsight-bank-mapping` · Builds on
`docs/features/matrix-project-routing-plan.md` (room ↔ project binding, per-conversation pin).

## 0. Decisions

Resolved with the user:

| # | Question | Decision |
|---|---|---|
| D1 | Turn usage | **Auto-recall from shared memory + project banks** every turn. **Auto-retain** is configurable **per project**: `retain_shared` (to the profile's shared `memory.provider`) defaults to **on**; `retain_project` (into the project's writable banks) defaults to **off**, meaning project banks are written externally. |
| D2 | Bank management | **Admin commands** manage banks: `/project memory …` in the room (Element `!project memory …`) and `hermes project memory …` on the CLI. `init` creates a bank on the Hindsight service and attaches it; `add`/`remove` attach or detach existing banks; `set` toggles the two retain flags. There is no auto-init on `/project create`, the agent cannot manage banks, and no bank mission is set. |
| D3 | Surfaces | **Gateway room-bound conversations only**, meaning conversations whose immutable project pin (from the existing routing) is bound. CLI, TUI and Desktop sessions are unchanged. |
| D4 | Placement | **Fork-local:** a generic core plus an in-tree backend. The core (projects.db tables, a `project_memory:` config section, turn wiring, commands) is provider-agnostic and talks to a small `ProjectMemoryBackend` protocol. Hindsight is the one backend, in one self-contained module. This is **not upstreamable as-is** (root rubric: "third-party products in the core tree"). The backend can move to a standalone plugin later without touching the core. |
| D5 | Recall tool | Add a **`project_memory_recall`** model tool. It exists **only** in project-bound conversations whose profile has project memory configured. |
| D6 | Service | **One self-hosted Hindsight URL per profile** (`project_memory.hindsight_url`), shared by every project in the profile. It is independent of `memory.provider` and of the catalog Hindsight plugin's `hindsight/config.json`. No cloud or embedded-daemon modes. |
| D7 | Multi-bank | **Every attached bank is recalled, in attach order.** Banks are queried in parallel; results are merged in order and deduped by text. A failing bank is skipped with a 5-minute cooldown. Auto-retain (when `retain_project` is on) writes only to banks attached as writable; `add --recall-only` attaches a bank that is never written. |

Designer defaults (accepted by the user at approval):

| # | Topic | Default |
|---|---|---|
| D8 | Meaning of "auto-retain to shared = off" | The automatic write hooks of the shared external provider are skipped for that project's conversations: `sync_turn`, `on_session_end`, `on_pre_compress` (including its checkpoint), and `on_delegation`. These keep working: recall (`prefetch`/`queue_prefetch`), `on_turn_start`, `on_session_switch`, the shared provider's own tools when the model calls them, and the mirror of **explicit** built-in `memory` tool writes (`on_memory_write`), because those are writes the model chose to make. The built-in MEMORY.md/USER.md store is never affected. |
| D9 | Live updates | The bank list and retain flags are **re-read from projects.db at the start of every turn**, so `/project memory add/remove/set` affects every conversation of that project from its next turn, including existing threads. This is cache-safe because neither banks nor flags appear in the system prompt or tool schemas (§3.6). Toggling the profile-level service config is different: it changes the tool surface, so it takes effect for new conversations (agent-cache rebuild) like any other config edit. |
| D10 | Default bank id for `init` | `<runtime-profile>-<project-slug>`, e.g. `default-alpha`, and the bank's Hindsight `name` is the project name. `init <bank-id>` overrides the id. Bank ids match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` and are URL-encoded on the wire. |
| D11 | API key | Optional secret `PROJECT_HINDSIGHT_API_KEY` in the profile `.env`, sent as `Authorization: Bearer` when set. A self-hosted server without auth needs nothing. This is a secret, so it is allowed in `.env` by the rubric. |
| D12 | Failure policy | Project memory **fails open.** Unlike the fail-closed workspace pin, memory is advisory: an unreachable service or bank gives an empty recall with one WARNING per outage, and the turn still runs. Retain failures are logged, never raised. An unset service with banks attached is reported by `/project memory`, never per turn. |
| D13 | `remove` / project delete | `remove` only detaches: nothing is deleted on the Hindsight server. Deleting a project cascades its bank rows and settings (unlike room bindings, nothing needs to stay visible). |

## 1. Goal and acceptance criteria

**Goal.** An admin attaches one or more banks on a profile-level, self-hosted Hindsight service to a Hermes project. Every gateway conversation pinned to that project then recalls from those banks every turn, alongside the shared memory provider, and gets a `project_memory_recall` tool. Each project controls whether turns are auto-retained to shared memory (default on) and into its own writable banks (default off). Banks can be created (`init`) and changed later (`add`/`remove`/`set`) from the room or the CLI, without a restart.

**Acceptance criteria.** Each one is proven by a test named in §4/§5.

- **AC1 – Combined recall.** In a bound room whose project has banks `[A (rw), B (recall-only)]`, the fake model's user-turn request contains the shared provider's recall, then a project-memory block whose A hits come before its B hits. Duplicate texts appear once.
- **AC2 – Recall tool.** In that conversation the request's tool list contains `project_memory_recall`. A scripted call returns hits from A and B (or only from `bank` when given), and the Hindsight stand-in records the recall requests.
- **AC3 – Retain defaults.** With default settings, a completed turn is recorded by the shared provider's `sync_turn`, and the stand-in records **no** retain.
- **AC4 – Retain toggles.** After `set retain-shared off` and `set retain-project on`, the next turn in the **same** thread is not recorded by the shared `sync_turn`, and the stand-in records exactly one retain, into A only (B is recall-only). `on_session_end` and pre-compress also skip the shared provider (unit).
- **AC5 – Cache invariant.** Across AC4's toggle and a bank `add`/`remove` within one thread, the system prompt and the tool list are byte-identical between consecutive requests. Only the user-turn memory context changes.
- **AC6 – Management.** `init` (the stand-in records `PUT /v1/default/banks/<id>` and the bank is listed), `add --recall-only`, `remove`, `set`, and info all work live with an unchanged gateway PID. They persist across a gateway restart, and `hermes project memory show <slug>` shows the same state from the CLI.
- **AC7 – Authorization.** With an admin list configured, a non-admin is refused `init`/`add`/`remove`/`set` but may run `/project memory` (info). An invalid bank id is refused. `init` without a configured service gives a clear reply and writes no DB row.
- **AC8 – Scope.** An unbound room, and a bound room in a profile without `project_memory.hindsight_url`, get no project block, no `project_memory_recall` tool, and shared-memory behaviour identical to `main`. The existing gateway and memory suites stay green.
- **AC9 – Fail open.** When the stand-in faults bank B (5xx or timeout), the turn still answers with A's hits, one WARNING is logged, and B is not re-queried within the cooldown (unit). When the whole service is down, the turn answers without a project block.
- **AC10 – Profile isolation.** Two profile homes in one process, each with its own projects.db, service URL and key, recall and retain only against their own service, banks and key (unit, two scoped homes, including the background retain thread).

## 2. Out of scope

- CLI, TUI, Desktop and cron sessions (D3). Their memory is unchanged.
- Cloud or embedded Hindsight, and a service per project (D6).
- Bank missions and dispositions, bank deletion, reordering (remove + add to reorder), and ingesting project files into banks.
- Agent-driven bank management, a `reflect` tool, and auto-init on `/project create` (D2, D5).
- Changing the catalog Hindsight plugin or the one-external-provider rule for plugins.

## 3. Design

### 3.1 Data model (runtime profile's `projects.db`, added to `projects_db.SCHEMA_SQL`)

```sql
CREATE TABLE IF NOT EXISTS project_memory_banks (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    bank_id     TEXT NOT NULL,
    position    INTEGER NOT NULL,          -- recall order, 0-based, dense
    writable    INTEGER NOT NULL DEFAULT 1, -- 0 = recall-only (never auto-retained into)
    added_by    TEXT,                       -- sender user id / "cli", audit only
    added_at    INTEGER NOT NULL,
    PRIMARY KEY (project_id, bank_id)
);

CREATE TABLE IF NOT EXISTS project_memory_settings (
    project_id      TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    retain_shared   INTEGER NOT NULL DEFAULT 1,
    retain_project  INTEGER NOT NULL DEFAULT 0,
    updated_by      TEXT,
    updated_at      INTEGER NOT NULL
);
```

A project with no settings row has the defaults. The tables are additive (`CREATE TABLE IF NOT EXISTS`), so a legacy DB upgrades on open.

### 3.2 Config (profile `config.yaml`; no non-secret env var, per the rubric)

```yaml
project_memory:
  hindsight_url: ""        # self-hosted Hindsight base URL for project banks; "" = project memory off
  recall_budget: mid       # low | mid | high (Hindsight recall budget)
  recall_max_tokens: 1024  # per bank, per recall
  timeout: 10              # seconds per Hindsight request (recall is bounded by min(timeout, 8s prefetch cap))
```

The optional secret `PROJECT_HINDSIGHT_API_KEY` lives in the profile `.env`. It is registered in `OPTIONAL_ENV_VARS` so `hermes setup`/`hermes config` know it, and is read only through `agent.secret_scope.get_secret`.

### 3.3 Module boundaries

```
hermes_cli/projects_db.py               + bank/settings tables + CRUD (pure SQLite)
agent/project_memory/                   NEW package, provider-agnostic core + one backend
  backend.py                            ProjectMemoryBackend Protocol, RecallHit, RetainItem, BackendError
  config.py                             ProjectMemoryServiceConfig, load_service_config(), API_KEY_ENV
  hindsight.py                          HindsightBackend (httpx, sync) — the ONLY Hindsight-aware file
  provider.py                           ProjectMemoryTarget, ProjectMemoryProvider(MemoryProvider), build_project_memory_provider()
  ops.py                                management operations shared by CLI + gateway
agent/memory_manager.py                 + provider slots ("external" | "project") + auto-retain policy seam
agent/agent_init.py (+ sibling)         attach the project provider in _init_memory (net growth ≤ 0)
run_agent.py / agent/agent_init_params  AIAgent(project_memory=...) keyword
gateway/project_routing_turn.py         project_memory_target_for_pin()
gateway/session_execution.py            pass target into AIAgent; signature input
gateway/run_agent_cache.py              _agent_config_signature(project_memory_id=)
gateway/run_background.py               same target for /background runs
gateway/slash_commands_project.py       route "memory" subcommand + admin gate
gateway/slash_commands_project_memory.py NEW: /project memory handlers
hermes_cli/projects_cmd.py              `hermes project memory …`
```

The dependency direction is `gateway`/`hermes_cli` → `agent.project_memory` → `hermes_cli.projects_db`. Only `hindsight.py` knows Hindsight's wire format, and nothing imports the catalog Hindsight plugin.

### 3.4 Data flow (messaging turn in a bound room)

```
_hmwa_prepare_turn → apply_turn_project_pin (unchanged; fail-closed pin)
_resolve_turn_agent:
    pin = project_pin_for_key(...)                                   (unchanged)
    target = project_memory_target_for_pin(runner, source, pin)      NEW → ProjectMemoryTarget | None
    sig = _agent_config_signature(..., session_cwd, project_memory_id=target.project_id if target else "")
    fresh agent → AIAgent(..., cwd=pin.cwd, project_memory=target)
agent_init._init_memory (inside the profile scope the agent is built for):
    shared provider (memory.provider) as today
    if target and not skip_memory:
        p = build_project_memory_provider(target)       # None when project_memory.hindsight_url unset
        manager = existing or MemoryManager(); manager.add_provider(p, slot="project")
        manager.set_auto_retain_policy(p.auto_retain_allows)
    initialize_for_agent(...); inject_memory_provider_tools(agent)   # adds project_memory_recall
turn (agent/turn_context.py, unchanged call sites):
    manager.on_turn_start → ProjectMemoryProvider refreshes snapshot {banks, settings} from target.projects_db
    manager.prefetch_all  → shared recall block, then project recall block (user-turn context, not system prompt)
    … model may call project_memory_recall → manager.handle_tool_call → provider
after turn (run_agent.py, unchanged call site):
    manager.sync_all → only providers where policy(provider) is True:
        shared external: settings.retain_shared ; project: settings.retain_project ; builtin: True
```

### 3.5 Interfaces

**`hermes_cli/projects_db.py`**

```python
_BANK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

@dataclass(frozen=True)
class ProjectMemoryBank:
    project_id: str
    bank_id: str
    position: int
    writable: bool
    added_by: Optional[str]
    added_at: int

@dataclass(frozen=True)
class ProjectMemorySettings:
    project_id: str
    retain_shared: bool = True
    retain_project: bool = False

def normalize_bank_id(bank_id: str) -> str: ...          # strip; ValueError("invalid bank id …") if no match
def list_memory_banks(conn: sqlite3.Connection, project_id: str) -> list[ProjectMemoryBank]: ...  # ORDER BY position
def add_memory_bank(conn: sqlite3.Connection, project_id: str, bank_id: str, *, writable: bool = True,
                    added_by: Optional[str] = None) -> tuple[ProjectMemoryBank, bool]:
    """Append (position = max+1) or, if already attached, update `writable` in place keeping its
    position. Returns (row, created). ValueError for an unknown project or invalid id.
    Joins a caller-owned txn or opens write_txn (same convention as set_chat_binding)."""
def remove_memory_bank(conn: sqlite3.Connection, project_id: str, bank_id: str) -> bool: ...  # re-densifies positions
def get_memory_settings(conn: sqlite3.Connection, project_id: str) -> ProjectMemorySettings: ...  # defaults if no row
def set_memory_settings(conn: sqlite3.Connection, project_id: str, *, retain_shared: Optional[bool] = None,
                        retain_project: Optional[bool] = None, updated_by: Optional[str] = None
                        ) -> ProjectMemorySettings: ...  # upsert; None leaves a field; ValueError unknown project
```

**`agent/project_memory/backend.py`**

```python
@dataclass(frozen=True)
class RecallHit:
    bank_id: str
    text: str
    id: str = ""
    kind: str = ""            # Hindsight result `type` (world/experience/observation…)
    occurred_at: str = ""

@dataclass(frozen=True)
class RetainItem:
    content: str
    context: str
    document_id: str
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)
    timestamp: str = ""       # ISO-8601

class BackendError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None) -> None: ...
    status: Optional[int]

class ProjectMemoryBackend(Protocol):
    def ensure_bank(self, bank_id: str, *, name: str) -> None: ...      # idempotent create
    def bank_exists(self, bank_id: str) -> bool: ...
    def recall(self, bank_id: str, query: str) -> list[RecallHit]: ...  # missing bank → []
    def retain(self, bank_id: str, items: Sequence[RetainItem]) -> None: ...
    def close(self) -> None: ...
```

**`agent/project_memory/config.py`**

```python
API_KEY_ENV = "PROJECT_HINDSIGHT_API_KEY"

@dataclass(frozen=True)
class ProjectMemoryServiceConfig:
    url: str                       # normalized, no trailing "/"
    recall_budget: str = "mid"
    recall_max_tokens: int = 1024
    timeout: float = 10.0

def load_service_config(config: Mapping[str, Any]) -> Optional[ProjectMemoryServiceConfig]:
    """From the `project_memory` section of an already-loaded config dict. None when the section or
    hindsight_url is empty. ValueError for a non-http(s) URL, unknown budget, or non-positive numbers
    (callers report it; the agent path logs once and runs without project memory)."""
```

**`agent/project_memory/hindsight.py`** (verified against the Hindsight OpenAPI at the catalog pin `d952b6e655`)

```python
class HindsightBackend:  # implements ProjectMemoryBackend
    def __init__(self, service: ProjectMemoryServiceConfig, *, api_key: str = "",
                 transport: Optional[httpx.BaseTransport] = None) -> None: ...
    # ensure_bank: PUT  {url}/v1/default/banks/{id}               body {"name": name}
    # bank_exists: GET  {url}/v1/default/banks/{id}/config        404 → False
    # recall:      POST {url}/v1/default/banks/{id}/memories/recall
    #              body {"query", "budget": service.recall_budget, "max_tokens": service.recall_max_tokens}
    #              → RecallResponse.results[].{id,text,type,occurred_start}; 404 → []
    # retain:      POST {url}/v1/default/banks/{id}/memories       body {"items": [...MemoryItem], "async": true}
    # All: quote(bank_id, safe=""), timeout=service.timeout, Bearer header iff api_key, any non-2xx or
    # httpx.HTTPError → BackendError(status=...). Uses agent.ssl_verify for TLS like other core httpx clients.

def backend_for_current_profile(service: ProjectMemoryServiceConfig) -> HindsightBackend:
    """api_key = get_secret(API_KEY_ENV, "") — read in the caller's profile scope."""
```

**`agent/project_memory/provider.py`**

```python
@dataclass(frozen=True)
class ProjectMemoryTarget:
    project_id: str
    slug: str
    name: str
    projects_db: Path          # explicit runtime-profile DB (runtime_projects_db), never projects_db_path()

PROJECT_MEMORY_RECALL_TOOL = "project_memory_recall"
# schema: {"query": str (required), "bank": str (optional; must be one of this project's banks)}
# description is STATIC (no bank ids, no project name).

class ProjectMemoryProvider(MemoryProvider):   # registered with add_provider(p, slot="project")
    def __init__(self, target: ProjectMemoryTarget, service: ProjectMemoryServiceConfig,
                 backend: ProjectMemoryBackend) -> None: ...
    @property
    def name(self) -> str: ...                      # "project"
    def is_available(self) -> bool: ...             # True (service presence decided by the builder)
    def initialize(self, session_id: str, **kwargs) -> None: ...   # keeps session_id/platform/chat ids for retain metadata
    def system_prompt_block(self) -> str: ...       # one static sentence (§3.6)
    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None: ...  # refresh snapshot (D9)
    def prefetch(self, query: str, *, session_id: str = "") -> str: ...
    def queue_prefetch(self, query: str, *, session_id: str = "") -> None: ...      # no-op
    def recall_status(self) -> Optional[RecallStatus]: ...    # "Project memory — recalled N memories"
    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", **kwargs) -> None: ...
    def get_tool_schemas(self) -> list[dict[str, Any]]: ...
    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str: ...  # JSON string
    def auto_retain_allows(self, provider: MemoryProvider) -> bool: ...
        # builtin → True; self → snapshot.retain_project; any other provider → snapshot.retain_shared
    def shutdown(self) -> None: ...                 # backend.close()

def build_project_memory_provider(target: Optional[ProjectMemoryTarget]) -> Optional[ProjectMemoryProvider]:
    """In the agent's profile scope: load_config() → load_service_config(); None when target is None or
    the service is unset/invalid (invalid logs one WARNING per home). Backend from backend_for_current_profile."""
```

Provider behaviour:
- **Snapshot.** `on_turn_start` opens `connect_closing(db_path=target.projects_db)` and reads `list_memory_banks` + `get_memory_settings`. The first turn loads it lazily if `on_turn_start` did not run. A read error keeps the previous snapshot and logs at DEBUG.
- **Recall** (`prefetch` and the tool). Banks run in parallel on a small `ThreadPoolExecutor` whose submits are wrapped with `agent.memory_provider.ctx_bound`, so the profile scope and secret scope carry into the workers. A bank still unanswered after 80% of the timeout counts as failed. The result is merged in bank order and deduped by normalized text. Each failed bank gets `(bank_id, "recall")` → cooldown 300s, with one WARNING when it goes down and one INFO line when it answers again.
- **Recall block format:**
  ```
  ## Project memory — <project name>
  [<bank_id>] <text>
  ```
  The output is capped at `recall_max_tokens × banks × 4` chars, and the manager's existing spill config applies on top. An empty result gives `""`.
- **`sync_turn`** retains one `RetainItem` into each **writable** bank: `content="User: …\nAssistant: …"`, `context="Hermes project <slug> conversation"`, `document_id=f"{session_id}-{turn_index}"`, `tags=("hermes", f"project:{slug}", f"session:{session_id}")`, and metadata `{platform, chat_id, thread_id}`. Retain cooldown is separate from recall cooldown. It runs on the manager's background worker (already `ctx_bound`).
- **The tool** with an unknown `bank` returns `tool_error`. With zero banks it returns `{"results": [], "note": "no banks attached"}`. The JSON shape is `{"results": [{"bank", "text", "kind", "occurred_at"}], "failed_banks": [...]}`.

**`agent/memory_manager.py`** (extend; no behaviour change when unused)

```python
def add_provider(self, provider: MemoryProvider, *, slot: Literal["external", "project"] = "external") -> None:
    # One provider per slot; builtin exempt. Plugins are always added by core code with the default
    # slot, so a plugin can never claim "project"; only attach_project_memory passes slot="project".
    # The existing "only ONE external" warning stays for the "external" slot.
def set_auto_retain_policy(self, policy: Optional[Callable[[MemoryProvider], bool]]) -> None: ...
def _auto_retain_providers(self) -> list[MemoryProvider]: ...   # providers where policy is None or policy(p) is True
# Gated through _auto_retain_providers (D8): sync_all, on_session_end (and so commit_session_boundary_async),
#   on_pre_compress, supports_pre_compress_checkpoint (counts only allowed providers, so compression never
#   demands a checkpoint from a suppressed provider), on_delegation.
# Not gated: prefetch_all, queue_prefetch_all, on_turn_start, on_session_switch, handle_tool_call, on_memory_write.
```

A policy that raises is treated as "allowed" for builtin and "denied" for others, with one WARNING. That fail-safe means no egress when in doubt.

**Agent wiring**

```python
# run_agent.py AIAgent.__init__: new keyword  project_memory: "ProjectMemoryTarget | None" = None
#   (forwarded through init_kwargs; add to agent_init_params passthrough handling as needed)
# agent/agent_init.py::_init_memory(agent, _agent_cfg, skip_memory, platform, memory_manager=None, project_memory=None)
#   creates the MemoryManager when EITHER a non-core memory.provider OR a buildable project provider exists.
#   agent_init.py is over the 2,000-line ratchet: net growth must be ≤ 0 — move the new attach logic into
#   agent/project_memory/provider.py::attach_project_memory(agent, manager, target) -> MemoryManager | None
#   and compact/move an existing helper (e.g. _memory_provider_init_kwargs → agent/agent_init_memory_provider.py,
#   updating its test imports; no re-export shim).
```

**Gateway glue**

```python
# gateway/project_routing_turn.py
def project_memory_target_for_pin(runner: Any, source: SessionSource, pin: Optional[ProjectPin]
                                  ) -> Optional[ProjectMemoryTarget]:
    """None unless pin is bound; ProjectMemoryTarget(pin.project_id, pin.slug, pin.name,
    runtime_projects_db(runner, source)). Pure (no I/O): the provider builder decides service presence."""

# gateway/run_agent_cache.py
def _agent_config_signature(..., session_cwd: str = "", project_memory_id: str = "") -> str
    # project_memory_id hashed: a different project means a different tool surface + prompt block.
# _extract_cache_busting_config: include the `project_memory` config section, so a service-config edit
# rebuilds agents (new tool surface) instead of silently keeping a stale one.

# gateway/session_execution.py: _resolve_turn_agent computes target from the same pin; _build_fresh_agent(...,
#   project_memory=target) passes AIAgent(project_memory=target).
# gateway/run_background.py: same target for /background agents (recall + same retain policy).
```

**Management operations** (`agent/project_memory/ops.py`, used by the CLI and gateway; no i18n inside)

```python
class ProjectMemoryOpError(ValueError):
    """reason ∈ {no_service, invalid_service, invalid_bank, service_error, not_attached}; params for messages."""
    def __init__(self, reason: str, **params: str) -> None: ...

@dataclass(frozen=True)
class MemoryOverview:
    service_url: Optional[str]          # None = not configured
    service_error: str                  # "" or the load_service_config error
    banks: list[ProjectMemoryBank]
    settings: ProjectMemorySettings

def overview(conn, project: Project, config: Mapping[str, Any]) -> MemoryOverview: ...
def init_bank(conn, project: Project, config: Mapping[str, Any], *, profile: str, bank_id: Optional[str] = None,
              actor: Optional[str] = None, backend: Optional[ProjectMemoryBackend] = None
              ) -> tuple[ProjectMemoryBank, bool]:
    """Default id f"{profile}-{project.slug}" (D10). Service first (ensure_bank, idempotent PUT), then
    add_memory_bank(writable=True) — a retry after a DB failure is safe. no_service / service_error raise."""
def attach_bank(conn, project: Project, bank_id: str, *, recall_only: bool, actor: Optional[str] = None,
                backend: Optional[ProjectMemoryBackend] = None) -> tuple[ProjectMemoryBank, bool, Optional[bool]]:
    """Returns (row, created, exists_on_service); exists is None when no service/backend (still attaches,
    banks may be pre-registered before the service is configured)."""
def detach_bank(conn, project: Project, bank_id: str) -> bool: ...
def update_settings(conn, project: Project, *, retain_shared: Optional[bool] = None,
                    retain_project: Optional[bool] = None, actor: Optional[str] = None) -> ProjectMemorySettings: ...
```

**Gateway command** (`/project memory …`; Element `!project memory …` via the existing bang mapping)

```
/project memory                              info: service URL or "not configured", banks in order with rw/recall-only,
                                             retain-shared/retain-project flags
/project memory init [<bank-id>]             admin; create on service + attach writable
/project memory add <bank-id> [--recall-only] admin; attach (warn when the service doesn't know the bank yet)
/project memory remove <bank-id>             admin; detach only (D13)
/project memory set retain-shared|retain-project on|off   admin
```

- The command acts on the **room's bound project**. If the room is unbound it replies `gateway.project.memory.not_bound`. A dangling binding reuses `info_dangling`.
- `slash_commands_project.py`: add `"memory"` to the handler table. The admin gate becomes "`create`/`bind`/`unbind`, or `memory` with a first arg in `{init, add, remove, set}`". Handlers live in the new `GatewayProjectMemoryCommandsMixin` (`gateway/slash_commands_project_memory.py`), composed into `GatewayProjectCommandsMixin`. They follow the same `_project_work` thread and profile-scope pattern, the same table dispatch, and the same `ProjectRouteError`/`OSError`/`sqlite3.Error` handling, plus `ProjectMemoryOpError` → `t(f"gateway.project.memory.error.{reason}", **params)`.
- The config comes from the runtime profile's config inside `_profile_scope_for_source`: `load_config()` there, and the profile name from the identity, as in `_project_info`.
- `gateway.project.usage` gains the memory line. `hermes_cli/commands.py`'s `args_hint` for `project` adds `memory`.

**CLI** (`hermes project memory …`, `hermes_cli/projects_cmd.py`)

```
hermes project memory show   <project>
hermes project memory init   <project> [--bank ID]
hermes project memory add    <project> <bank> [--recall-only]
hermes project memory remove <project> <bank>
hermes project memory set    <project> [--retain-shared on|off] [--retain-project on|off]
```

These use the same `ops` functions with `actor="cli"` and the profile from `hermes -p`. Output is plain English like the rest of `projects_cmd.py`. `ProjectMemoryOpError` → `project: …` with exit code 2.

### 3.6 Invariants and conventions

- **Prompt caching.**
  - The provider's `system_prompt_block()` is a fixed sentence with no project name, bank ids or flags: "This conversation belongs to a Hermes project with its own memory banks; relevant project memories are attached to each user message, and `project_memory_recall` searches them."
  - The tool schema is static.
  - Provider presence is fixed per conversation, because the pin is immutable and the signature includes `project_memory_id`.
  - Bank or flag changes alter only the per-turn memory context, which already lives in the user-turn content via `build_memory_context_block`. The system prompt is never rebuilt.
- **Profile isolation.**
  - The DB path is explicit (`runtime_projects_db`), and the config is read in the agent's build-time profile scope.
  - The API key comes from `get_secret` at call time, and each recall and background thread is `ctx_bound`.
  - There is no module-level mutable state: cooldown dicts live on the provider instance.
  - The `_StuckCallGate` keys already include the home.
- **Egress.** Every hand-off goes through the manager's existing `_redact_for_provider`: prefetch query, tool args, `sync_turn` strings. The provider adds no other egress.
- **Footprint.**
  - There is no new core tool in `_HERMES_CORE_TOOLS` or `toolsets.py`. The tool is a memory-provider tool routed by `MemoryManager`, so it follows the existing `memory` toolset gate (`memory_provider_tools_exposed`) and exists only where the provider was attached. Surface is decided by the session's pin, never by env.
  - The only new env var is a secret.
- **Code health.**
  - New functions: CC ≤ 20 and ≤ 300 lines.
  - Subcommand tables, not ladders.
  - `agent_init.py` (over the cap) has net growth ≤ 0. `config_defaults.py` has net growth ≤ 0 if it is over its ratchet: compact neighbouring comments.
  - No `try/except: pass`. Comments say why, not what.
- **i18n.** Every new `gateway.project.memory.*` key and the updated `gateway.project.usage` go into all 17 `locales/*.yaml` (`tests/agent/test_i18n.py` parity).

## 4. Tasks and waves

The environment comes first; E2E needs `mautrix`:
`python -m pm.build_env --source . --out .venv --group dev --group test --extra matrix`

### Wave 1 (parallel; disjoint files, no cross-imports)

**T1 — Project memory store** (`hermes_cli/projects_db.py`)
- **Goal:** the §3.1 tables and the §3.5 `projects_db` API.
- **Owns:** `hermes_cli/projects_db.py`, `tests/hermes_cli/test_projects_db_memory.py` (new).
- **Produces:** `ProjectMemoryBank`, `ProjectMemorySettings`, `normalize_bank_id`, `list_/add_/remove_memory_bank`, `get_/set_memory_settings`.
- **Consumes:** existing `write_txn`, `connect`.
- **Depends:** none.
- **Tests:**
  - Append order, and positions re-densify after a remove.
  - Re-adding updates `writable` but keeps the position.
  - Invalid ids (`../x`, spaces, `""`, 129 chars) are rejected.
  - An unknown project gives `ValueError`.
  - Settings default without a row; partial updates.
  - Deleting a project cascades both tables.
  - A legacy DB without the tables upgrades on open.
  - Joining a caller transaction.
- **Unit:** `scripts/run_tests.sh tests/hermes_cli/test_projects_db_memory.py tests/hermes_cli/test_projects_db_bindings.py`

**T2 — Service config + Hindsight backend**
- **Goal:** `agent/project_memory/{__init__,backend,config,hindsight}.py`, the config default, the secret registration, and a reusable Hindsight HTTP stand-in.
- **Owns:**
  - `agent/project_memory/__init__.py`, `backend.py`, `config.py`, `hindsight.py` (new)
  - `hermes_cli/config_defaults.py` (`project_memory` section) and `hermes_cli/config.py` (known top-level key, plus `OPTIONAL_ENV_VARS` entry `PROJECT_HINDSIGHT_API_KEY`)
  - `tests/fakes/hindsight_standin.py` (new): a threaded `http.server` on loopback that records requests and serves `PUT banks/{id}`, `GET banks/{id}/config`, `POST …/memories/recall` and `POST …/memories`. Seeded results per bank, queued faults per bank (status/delay), `requests(path_prefix)`, unknown routes → 404 recorded.
  - `tests/agent/test_project_memory_backend.py` (new)
- **Produces:** `ProjectMemoryBackend`, `RecallHit`, `RetainItem`, `BackendError`, `ProjectMemoryServiceConfig`, `load_service_config`, `API_KEY_ENV`, `HindsightBackend`, `backend_for_current_profile`, `HindsightStandin`.
- **Depends:** none.
- **Tests** (against the stand-in through real httpx, not mocks):
  - The wire shape of each call.
  - The Bearer header only when the key is set.
  - Bank id URL-encoding.
  - A recall 404 gives `[]`; 5xx and timeouts give `BackendError(status)`.
  - `load_service_config` validation.
  - `backend_for_current_profile` reads the key from a scoped temp home `.env` (two homes give two keys).
  - A config written to a temp `config.yaml` reaches `load_service_config(load_config())`.
- **Unit:** `scripts/run_tests.sh tests/agent/test_project_memory_backend.py tests/hermes_cli/test_config.py`

**T3 — MemoryManager slots + auto-retain policy** (`agent/memory_manager.py`)
- **Goal:** the §3.5 manager changes.
- **Owns:** `agent/memory_manager.py`, `tests/agent/test_memory_manager_retain_policy.py` (new).
- **Produces:** `MemoryManager.add_provider(..., slot=)`, `MemoryManager.set_auto_retain_policy`, and gated hooks.
- **Consumes:** none new.
- **Depends:** none.
- **Tests** (real `MemoryManager` with recording fake providers):
  - A second `external` is still rejected; `external` + `project` coexist; a second `project` is rejected.
  - With a policy denying the external provider: no `sync_turn`, `on_session_end`, `on_pre_compress` or `on_delegation` reaches it, while `prefetch`, `on_turn_start`, tools and `on_memory_write` still do.
  - `supports_pre_compress_checkpoint` ignores denied providers.
  - A policy that raises denies non-builtin.
  - With no policy, behaviour equals `main` (existing memory suites green).
- **Unit:** `scripts/run_tests.sh tests/agent/test_memory_manager_retain_policy.py tests/agent/test_memory_provider.py tests/agent/test_memory_async_sync.py tests/agent/test_memory_session_switch.py`

### Wave 2 (parallel; T4 and T5 own disjoint files)

**T4 — Project memory provider + agent/gateway turn wiring**
- **Goal:** `ProjectMemoryProvider`, the builder/attach, the `AIAgent(project_memory=)` keyword, and the gateway passing the target for messaging and `/background` turns (§3.4).
- **Owns:**
  - `agent/project_memory/provider.py` (new)
  - `run_agent.py` (one keyword), `agent/agent_init_params.py`, `agent/agent_init.py` (net ≤ 0), `agent/agent_init_memory_provider.py` (receives the moved helper)
  - `gateway/project_routing_turn.py`, `gateway/session_execution.py`, `gateway/run_agent_cache.py`, `gateway/run_background.py`
  - Tests whose imports change due to the helper move (`tests/agent/test_memory_*` imports only)
  - `tests/agent/test_project_memory_provider.py` (new), `tests/gateway/test_project_memory_turn_wiring.py` (new)
- **Consumes:** T1 (store reads), T2 (`load_service_config`, `backend_for_current_profile`, `HindsightStandin`), T3 (slot + policy).
- **Produces:** `ProjectMemoryTarget`, `ProjectMemoryProvider`, `build_project_memory_provider`, `attach_project_memory`, `PROJECT_MEMORY_RECALL_TOOL`, `project_memory_target_for_pin`, `_agent_config_signature(project_memory_id=)`.
- **Depends:** T1, T2, T3.
- **Tests:**
  - Provider against the stand-in and a real temp projects.db:
    - Ordered merge and dedup.
    - Recall-only banks are never retained into.
    - The snapshot refreshes on `on_turn_start` after `add`/`remove`/`set`.
    - Cooldown: B failing is skipped within 300s and the WARNING appears once.
    - Tool: `bank` filter, unknown bank, zero banks.
    - `system_prompt_block` and the tool schema are identical across bank and flag changes (AC5 unit form).
  - AC10: two scoped homes in threads, each recall/retain reaching its own stand-in with its own key.
  - Wiring:
    - A real `AIAgent` built with `project_memory=target` and `memory.provider` unset has a manager with the project provider and `project_memory_recall` in `agent.tools`.
    - Without a service URL there is no provider and no tool.
    - With `memory` disabled in toolsets the tool is withheld.
    - `_agent_config_signature` differs by `project_memory_id`.
    - `_resolve_turn_agent` passes the target for a bound pin and `None` for unbound and LOCAL.
- **Unit:** `scripts/run_tests.sh tests/agent/test_project_memory_provider.py tests/gateway/test_project_memory_turn_wiring.py tests/agent/ tests/gateway/test_project_turn_wiring.py`

**T5 — Management surfaces: ops, `/project memory`, CLI, i18n, docs**
- **Goal:** §3.5 "Management operations", the gateway command, the CLI, and user docs.
- **Owns:**
  - `agent/project_memory/ops.py` (new)
  - `gateway/slash_commands_project.py`, `gateway/slash_commands_project_memory.py` (new)
  - `hermes_cli/projects_cmd.py`, `hermes_cli/commands.py` (`args_hint`)
  - `locales/*.yaml` (`gateway.project.memory.*`, `gateway.project.usage`)
  - `website/docs/user-guide/messaging/matrix.md` ("Project rooms" → "Project memory banks" subsection covering config, `.env` key, commands, retain flags, fail-open, D8 semantics, the fork-local note)
  - `website/docs/reference/slash-commands.md`; `website/docs/reference/environment-variables.md` (`PROJECT_HINDSIGHT_API_KEY`); the config reference page that documents `gateway.project_routing` (`git grep -l project_routing website/docs`)
  - `website/docs/developer-guide/gateway-command-parity.md` (regenerate if `args_hint` feeds it: `python scripts/gen_gateway_command_parity.py`)
  - `tests/gateway/test_project_memory_command.py` (new), `tests/hermes_cli/test_projects_cmd_memory.py` (new)
- **Consumes:** T1 store and T2 config/backend/stand-in. Existing: `policy_for_runner_source`, `_profile_scope_for_source`, `binding_key`, `runtime_projects_db`.
- **Produces:** `ops.*`, `ProjectMemoryOpError`, `MemoryOverview`, `GatewayProjectMemoryCommandsMixin`, CLI `hermes project memory`.
- **Depends:** T1, T2. Not T4: ops and commands never import `provider.py`.
- **Tests:**
  - Each subcommand through the real `_handle_project_command` against a temp home with a real projects.db and the stand-in.
  - AC7: admin gating with `group_allow_admin_from`, info is allowed for a non-admin, invalid ids.
  - `init` without a service writes no row.
  - `init` with a stand-in 500 writes no row; a retry after a DB error is idempotent.
  - `add` of an unknown-to-service bank attaches with a warning.
  - An unbound room is refused.
  - The bang mapping: `_normalize_matrix_bang_command("!project memory add kb") == "/project memory add kb"`.
  - CLI `show`/`init`/`add`/`remove`/`set` with exit codes against a temp `HERMES_HOME`.
- **Unit:** `scripts/run_tests.sh tests/gateway/test_project_memory_command.py tests/gateway/test_project_command.py tests/hermes_cli/test_projects_cmd_memory.py tests/hermes_cli/test_gateway_command_parity.py tests/agent/test_i18n.py`

### Wave 3

**T6 — Feature E2E journey + sandbox E2E instructions**
- **Goal:** the §6 journey as one module, a recording shared-memory provider plugin for the sandbox, and the `tests/AGENTS.md` instructions.
- **Owns:**
  - `tests/e2e/core/platforms/test_matrix_project_memory.py` (new)
  - `tests/fakes/memory_recorder_plugin/plugin.yaml`, `tests/fakes/memory_recorder_plugin/__init__.py` (new; a `MemoryProvider` copied into the sandbox `HERMES_HOME/plugins/recorder/`, activated by `memory.provider: recorder`). It returns a fixed `SHARED_FACT` from `prefetch` and appends every `sync_turn`/`on_session_end`/`on_pre_compress` call as a JSON line to `$HERMES_HOME/recorder.jsonl`.
  - `tests/AGENTS.md` (the "Sandbox feature E2E" section gains this journey's command and the §6.2 policy text). Root `AGENTS.md` is **not** touched: it is at its cap, and the existing pointer already routes here.
- **Consumes:** T2 `HindsightStandin`; existing `GatewayUnderTest`, `MatrixDriver`, Matrix stand-in, `FakeLLMServer`/`Director`.
- **Depends:** T4, T5.
- **Unit/E2E:** `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/e2e/core/platforms/test_matrix_project_memory.py -v`

## 5. Test and verification

| Check | Command | When |
|---|---|---|
| Unit (per task) | the task's `scripts/run_tests.sh …` line | every task |
| Unit regression | `scripts/run_tests.sh tests/agent/ tests/gateway/ tests/hermes_cli/` | after Wave 2 and before handoff |
| Full suite | `scripts/run_tests.sh` | before merge |
| Lint, ratchets, docs caps | `python scripts/check` (ruff, code-health ratchets: CC/size/`agent_init.py` net ≤ 0, AGENTS.md caps, HX rules) | every task before commit |
| Types | `python scripts/check` covers the repo's pinned type checks. Additionally, each new `agent/project_memory/*.py` is fully annotated (dataclasses + Protocol); `mypy agent/project_memory` must be clean if mypy is in the pinned toolset | T2, T4, T5 |
| i18n parity | `scripts/run_tests.sh tests/agent/test_i18n.py` | T5 |
| Command parity doc | `python scripts/gen_gateway_command_parity.py` then `git diff --exit-code website/docs/developer-guide/gateway-command-parity.md` after commit | T5 |
| Docs build | `cd website && npm run build` (if the toolchain is available; otherwise report it as skipped) | T5 |
| Lock | none: no new dependency (httpx is already core) | — |
| Feature E2E | §6 command | T6, once at feature introduction |

All tests are behaviour contracts on temp `HERMES_HOME` (autouse fixture). They make no network calls beyond the loopback stand-ins, read no source, and snapshot no counts.

## 6. E2E journey (sandbox)

### 6.1 Journey: `tests/e2e/core/platforms/test_matrix_project_memory.py`

**Setup** (all inside pytest's tmp dir):
- Matrix stand-in with rooms R1 (bound) and R2 (unbound), users `@admin` and `@guest`.
- Hindsight stand-in on loopback, seeded:
  - `default-alpha` → `ALPHA_FACT`
  - `team-kb` → `TEAMKB_FACT`, plus a duplicate `ALPHA_FACT` (for dedup)
- `FakeLLMServer` + `Director`.
- The recorder shared-memory plugin copied into the HOME.
- `config.yaml`:
  - `memory.provider: recorder`
  - `project_memory.hindsight_url: <standin>`
  - `gateway.project_routing.workspace_root: <tmp>/ws`
  - an admin list containing `@admin`
- `.env`: `PROJECT_HINDSIGHT_API_KEY=test-key`.
- Call `MatrixDriver.arm_readiness()` before every `GatewayUnderTest.start()`.

| # | User step | Expected result |
|---|---|---|
| 1 | `@admin` in R1: `!project create alpha` | Bound reply (existing feature). |
| 2 | `!project memory` | The reply shows the service URL, "no banks", retain-shared **on**, retain-project **off**. |
| 3 | `!project memory init` | The stand-in records `PUT /v1/default/banks/default-alpha` with `Authorization: Bearer test-key` and `{"name": "alpha"}`. The reply lists `default-alpha (read-write)`. |
| 4 | `!project memory add team-kb --recall-only` | The reply lists `default-alpha`, then `team-kb (recall-only)`. |
| 5 | `@guest`: `!project memory add x`, then `!project memory` | The first is refused (admin only); the info is shown. |
| 6 | `@admin`: thread T1 "what do we know?" | The model request's user-turn context contains `SHARED_FACT`, then `ALPHA_FACT`, then `TEAMKB_FACT`, with `ALPHA_FACT` exactly once (AC1). The tools include `project_memory_recall`. The reply lands in T1. `recorder.jsonl` gains one `sync_turn` (AC3). The stand-in has 2 recalls and 0 retains. |
| 7 | T1: the model is scripted to call `project_memory_recall{"query":"kb","bank":"team-kb"}` | The tool result in the next model request contains only `team-kb` hits (AC2). |
| 8 | `!project memory set retain-shared off`, `!project memory set retain-project on`; T1 "next" | No new `sync_turn` in `recorder.jsonl`. The stand-in records exactly one `POST …/default-alpha/memories` and none to `team-kb` (AC4). This request's system prompt and tool list are byte-equal to step 6's (AC5). |
| 9 | `!project memory remove team-kb`; T1 "again" | There is no `TEAMKB_FACT` in the context. The system prompt and tools are still byte-equal (AC5). The gateway PID is unchanged since step 1 (AC6). |
| 10 | Stand-in faults `default-alpha` recall with a 500; T1 "still there?" | The reply arrives and the context has no project hits. The gateway stays alive (AC9). Then clear the fault. |
| 11 | R2 (unbound): "hi" | No project block and no `project_memory_recall` tool. `recorder.jsonl` gains a `sync_turn` (AC8). |
| 12 | Stop and start the gateway (re-arm readiness); T1 "after restart" | `ALPHA_FACT` is recalled, retain-project is still on (one more retain to `default-alpha`), and the reply lands in T1 (AC6). |
| 13 | `hermes project memory show alpha` (subprocess, same HOME) | It prints `default-alpha (read-write)`, retain-shared off, retain-project on (AC6). |

Assertions are on stand-in deliveries, recorded model requests, stand-in request logs, and `recorder.jsonl`, never on log text. `finally` stops the gateway and both stand-ins.

### 6.2 Sandbox E2E policy (text added to `tests/AGENTS.md`)

> **When to run.** The sandbox feature E2E for a feature runs **when the feature is first added** (its introducing PR or branch must show it passing with the platform extra installed). **Future pushes and deployments do not require re-running it.** Existing unit tests, including the feature's own unit tests, still run on every push via `scripts/run_tests.sh` and `python scripts/check`. Re-run the journey when you change the feature's turn wiring, its stand-ins, or the gateway paths it drives.
>
> ```bash
> python -m pm.build_env --source . --out .venv --group dev --group test --extra matrix
> HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/e2e/core/platforms/test_matrix_project_memory.py -v
> ```

## 7. Open notes for the orchestrator

- D4 makes this fork-local: the Hindsight backend in core conflicts with the upstream rubric. It is isolated in `agent/project_memory/hindsight.py` behind `ProjectMemoryBackend`, so it can be extracted later.
- Only core `attach_project_memory` passes `slot="project"`; plugin providers keep the one-external rule.
- `agent_init.py` is over the file-size ratchet; T4 must keep its net growth ≤ 0.
