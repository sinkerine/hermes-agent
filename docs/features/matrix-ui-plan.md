# Matrix interactive UI — Hermes producer and action bridge (H1)

Status: **approved by the user** (2026-11-03). Branch `matrix-interactive-ui`, base `main` at
`a81a7148316ff60c69f0f9df6f5ce1ed631364fe`. Worktree `/home/sinkerine/.paseo/worktrees/15zkmyhp/matrix-interactive-ui`.

Protocol reference (read only): `/home/sinkerine/dev/element-web` at `bcb080171cb312feef37e63007c7ca8cc0a8b77f`
(`docs/features/interactive-ui-plan.md` §2.3/§2.4/§2.6/§3.5/§5B, `interactive-ui-verification.md`,
`apps/web/src/interactive-ui/{protocol,state,action}.ts`, `schema/interactive-ui-v1.schema.json`, `fixtures/`).
Where Web prose and the executable parser/fixtures disagree, the parser and fixtures win. This plan changes no wire
format.

OpenAI's text and Responses APIs do not expose ChatGPT's native Intelligent UI. The producer is an ordinary
structured tool call that the model fills in. Nothing here scrapes ChatGPT or passes private formats through.

## 1. Goal and acceptance criteria

### Goal

A GPT model running in Hermes on Matrix can answer with a first-class interactive card (`present_ui`). Hermes
validates the card against the v1 contract, sends it as one `m.room.message` with a complete text fallback, and
revises it in place with `m.replace`. When a user presses a backend action in Element Web, Hermes authenticates it,
validates it against its own latest revision, deduplicates it, confirms single-use actions at once, and starts exactly
one model turn in the same conversation.

### Acceptance criteria (each one is tested; "E2E" means the §5 sandbox journey)

1. **Conformance.** The Python parser (`parse_interactive_ui(raw, strict=False)`) accepts all 22 vendored valid
   fixtures and rejects each of the 53 invalid fixtures with exactly the `{path, code}` in its `*.expected.json`. In
   producer mode (`strict=True`), valid fixtures pass except `content.json`, whose deliberate unknown node is
   rejected at `nodes[9].type` with `bad_type`; every invalid fixture still fails with the same first error. A seeded mutation fuzz of the fixtures (≥ 2 000 cases) never raises.
2. **Producer limits.** The canonical payload (UTF-8, compact JSON) is at most 40 000 bytes. The whole UI event
   content is at most 44 000 bytes, which leaves room for a later status edit under the 45 000-byte Matrix cap
   (`_MAX_CONTENT_BYTES`). Every revision edit is at most 45 000 bytes. If a payload is over a limit, nothing is
   sent and the model gets a tool error. Hermes never truncates and never sends part of a card.
3. **Model-correctable errors.** Every rejection is returned as
   `tool_error("interactive_ui: <path>: <code>: <hint>")`, where the path and code are the parser's and the hint names
   the limit or the missing reference. In E2E, the model corrects at least one deliberately invalid payload within one
   retry.
4. **Event shape.** A new UI is one `m.room.message`. It carries `msgtype: m.text`, the complete Markdown `body`,
   `format`, a sanitised `formatted_body`, `"m.mentions": {}`, the conversation's thread relation if any, and
   `net.15cm.matrix.interactive_ui` (version 1, a Hermes-generated `ui_id`, `revision` 0, `status: active`). A revision
   is an `m.replace` edit with top-level `body: "* <summary|title>"` and no top-level payload. Its `m.new_content`
   holds the full content above, with a regenerated fallback and `revision + 1`.
5. **Complete fallback.** The deterministic Markdown fallback includes every node's text, every table row and cell,
   every chart value, every graph node, detail and edge (`A → B (label)`), every option, tab, collapsible body and
   conditionally visible branch (labelled with its condition), every link URL, image alt text, and every action label.
   A test walks each valid fixture and checks that each user-visible string and number appears in `body`.
6. **Sanitised content.** `rich_text.html` is passed through the adapter's existing Matrix HTML sanitiser before
   sending. `formatted_body` comes from the existing Markdown pipeline. The tool refuses `http:` links, remote image
   URLs and generated code. No tool result or log line contains tokens, credentials or payload text.
7. **Surface gating and caching.** `present_ui` is in the plugin toolset `interactive_ui`. It is off by default and
   can only be enabled for the `matrix` platform. The toolset is fixed when the agent is built, so enabling it applies
   from the next session or `/new`. It is never added mid-conversation. Sessions on other platforms, CLI/TUI/Desktop
   and delegated subagents never see the tool through ordinary named platform configuration. The explicit `all`
   override retains its existing passthrough semantics and may expose its schema. If one calls it anyway through
   that override, it gets a clear
   "Matrix conversations only" error and nothing is sent.
8. **Action authentication before side effects.** Every inbound `net.15cm.matrix.interactive_ui.action` reply passes
   the existing guards first: not self, not a bridge or system sender, not an ignored pattern, an allowed room, a
   fresh event, startup grace, and mention and thread gating. It also needs live sender authorisation
   (`_is_authorized_user`). Only then can it cause a notice, an edit or a turn. An unauthorised sender gets no notice,
   no edit and no turn.
9. **Server-authoritative validation.** An action is accepted only when all of the following hold:
   - `ui_event_id` is a Hermes-authored UI event in the same room, and it is the original event id, not an edit id.
   - The UI belongs to the same conversation, meaning the session key derived for the action equals the key stored
     for the UI. This also enforces profile, thread and per-user group scope.
   - `ui_id` matches, `revision` equals the latest revision Hermes authored, and the status is `active`.
   - `action_id` belongs to a reachable submit source (a button, a carousel item or a form).
   - `values` has exactly that source's declared keys (its `include` list, or the bound keys of the form's
     descendants), and each value passes `accepts_state_value` and the required-text checks.
   - The nonce is base64url and at least 22 characters long.

   Hermes never takes intent from the reply's `body` and never calls a tool named in the payload.
10. **Dedupe and atomicity.** A `(ui_event_id, nonce)` pair is accepted at most once. This holds across concurrent
    deliveries in one sync batch, across retries and across a gateway restart, because acceptance is a single SQLite
    `BEGIN IMMEDIATE` transaction under a per-adapter lock. A duplicate is dropped silently: no notice and no turn.
11. **Immediate confirmation.** For a `single_use` action (a form by default, or a button or item with
    `single_use: true`), the acceptance commit is followed at once by an `m.replace` with `revision + 1`,
    `status: submitted` and a localised `status_text`. This happens **before** the turn is dispatched. A repeatable
    action produces no edit. In E2E, the status edit reaches Element within the client's 8 s window.
12. **Exactly one turn.** Each accepted action dispatches exactly one `MessageEvent` through `handle_message`. It
    bypasses text batching, carries the thread relation of the action reply, and includes a text that Hermes builds
    from the stored payload (labels, `ui_id`, revision, action id and values). Stale, inactive, unknown and invalid
    actions from authorised users get one short localised `m.notice` reply in the same thread and no turn.
13. **Restart safety.** UI records and nonces live in `<matrix store dir>/interactive_ui.sqlite3`, pinned per profile
    when the adapter connects. After a restart, duplicates and stale actions are still rejected. If the record is
    missing, Hermes rebuilds it from the room: it fetches and decrypts the original event and the bot-authored
    `m.replace` edits, and accepts only events whose sender is its own `mxid`.
14. **Encrypted rooms.** In E2E, the full journey works in an encrypted room: card, interaction, status edit and
    model continuation. Uploaded images use the encrypted `file` form there and `mxc://` `url` elsewhere.
15. **Unsupported clients.** With the Element labs flag off, the same events show the complete fallback, which is a
    meaningful answer on its own (checked in E2E with a screenshot).
16. **Regressions.** Tests cover event replay, duplicate nonce, two concurrent actions, restart with a new adapter
    instance on the same store, the missing-record room fetch, encrypted content round trips through the fake client,
    two profiles that cannot act on each other's UIs, cross-room spoofing, edit-id spoofing and non-bot-authored
    targets.

## 2. Design

### 2.1 Constraints taken from the code (verified at the base commit)

- `plugins/platforms/matrix/adapter.py` has 3 323 lines. The code-health ratchet allows it at most its base length
  (`scripts/code_health/compare.py`: `lines <= max(2000, base)`). All new behaviour goes in siblings, and the hooks
  that `adapter.py` gains are paid for by moving existing HTML/Markdown helpers out (T3).
- Sends: `_send_room_message`, `_send_content_event`, `_build_text_message_content`, `_apply_relation_metadata`,
  `_content_bytes`, `_MAX_CONTENT_BYTES = 45_000`. Edits: `edit_message` (text only; it builds the `m.new_content`
  shape we reuse). Encryption happens transparently in `client.send_message_event`.
- Inbound: `_on_room_message` runs the guards (self, bridge, ignore patterns, allowed rooms, `_is_duplicate_event`,
  startup grace, skip `m.replace`), then dispatches on `msgtype`. `_resolve_message_context` handles mentions,
  threads, DMs and `build_source`. `_build_inbound_event` builds a `MessageEvent`. `handle_message` and
  `_enqueue_text_event` come from the base adapter. `_is_authorized_user(user_id, room_id)` uses live gateway
  authorisation. `_try_secret_capture` runs **before** the guards for any `m.text`.
- Session keys: `BasePlatformAdapter._source_session_key(source)` canonicalises identity first and includes the
  profile and `group_sessions_per_user`.
- Tools reach a conversation through the generic seam `PluginContext.post_message`
  (`hermes_cli/plugin_messages.py` → `gateway/plugin_messages.resolve`). It resolves the session bound in the calling
  context with `runner._delivery_adapter_for(source)`, checks profile ownership
  (`_plugin_messaging_session_owned`), gets thread metadata (`_thread_metadata_for_source`), and runs the work on the
  gateway loop. It is text-only today.
- Toolsets: plugin toolsets are on by default unless listed in `hermes_cli/tools_config.py::_DEFAULT_OFF_TOOLSETS`
  (precedent: `a2a`). Platform restriction is the table `hermes_cli/toolset_scope.py::_TOOLSET_PLATFORM_RESTRICTIONS`
  (precedent: `discord`). `_enabled_plugin_toolsets` currently does **not** apply that table, which is a bug for
  any restricted plugin toolset, and T2 fixes it.
- Locales: `locales/*.yaml` is checked for parity (`tests/agent/test_i18n.py`), so every new `platform.matrix.*` key
  goes into every catalogue.
- Package data: vendored runtime JSON needs a `pyproject.toml` `[tool.setuptools.package-data]` entry. The precedent
  is `config_backends/remote/vendor/**/*`.

### 2.2 Module boundaries

Everything specific to the protocol lives in the Matrix plugin. Core gains one generic plugin-context method and two
table rows.

```
plugins/platforms/matrix/
├── adapter.py                 # facade: +controller init in connect(), +2 hooks, +register_tool call; net ≤ 0 lines
├── adapter_html.py            # NEW (T3): moved sanitiser/markdown/latex helpers + markdown_to_matrix_html()
└── interactive_ui/            # NEW package
    ├── __init__.py            # constants only (keys, version, limits)
    ├── protocol.py            # T1: schema-interpreting parser port + relations + state helpers
    ├── vendor/element-web-bcb0801/   # T1: schema, fixtures, README (source commit, licence)
    ├── fallback.py            # T4: deterministic Markdown fallback
    ├── content.py             # T4: event/edit content builders + byte budgets
    ├── store.py               # T5: SQLite records + nonces (atomic)
    ├── submission.py          # T5: server-side action/value validation (port of validateSubmission semantics)
    ├── media.py               # T6: image upload resolution (mxc / EncryptedFile)
    ├── producer.py            # T6: present/revise on the adapter (lock, send, commit)
    ├── tool.py                # T6: PRESENT_UI_SCHEMA, handler, register_present_ui(ctx)
    └── inbound.py             # T7: action pipeline, notices, turn dispatch, room-fetch recovery
```

Core changes (T2): `hermes_cli/plugins.py` (`PluginContext.deliver_to_conversation`),
`hermes_cli/plugin_messages.py` (its implementation), `plugin_isolation.HOST_UNSUPPORTED_CTX_METHODS` (a live adapter
cannot cross the host boundary), `hermes_cli/toolset_scope.py` (`"interactive_ui": {"matrix"}`),
`hermes_cli/tools_config.py` (`_DEFAULT_OFF_TOOLSETS += "interactive_ui"`; `_enabled_plugin_toolsets` honours
platform restrictions), and the plugin developer docs.

### 2.3 Interfaces (exact)

```python
# plugins/platforms/matrix/interactive_ui/__init__.py
INTERACTIVE_UI_KEY = "net.15cm.matrix.interactive_ui"
INTERACTIVE_UI_ACTION_KEY = "net.15cm.matrix.interactive_ui.action"
INTERACTIVE_UI_VERSION = 1
PRODUCER_MAX_PAYLOAD_BYTES = 40_000
CLIENT_MAX_PAYLOAD_BYTES = 60_000
NEW_EVENT_MAX_CONTENT_BYTES = 44_000      # 45_000 cap minus status-edit headroom
TOOLSET = "interactive_ui"

# protocol.py
ErrorCode = Literal["not_object", "bad_version", "too_large", "missing_field", "bad_type", "bad_id",
                    "duplicate_id", "unknown_ref", "cycle", "too_deep", "too_many", "too_long",
                    "bad_value", "bad_url", "type_mismatch"]
@dataclass(frozen=True)
class InteractiveUiError:
    path: str
    code: ErrorCode
    hint: str = ""                          # producer-only, never sent on the wire
@dataclass(frozen=True)
class ParsedUi:
    payload: dict[str, Any]                 # trimmed, unknown props dropped, unknown types → {"type": "unknown", ...}
    nodes_by_id: Mapping[str, dict[str, Any]]   # reachable nodes only
def parse_interactive_ui(raw: object, *, strict: bool = False) -> ParsedUi | InteractiveUiError
def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes   # ensure_ascii=False, separators=(",", ":"), allow_nan=False
def child_ids(node: Mapping[str, Any]) -> list[str]
def state_kind(payload: Mapping[str, Any], key: str) -> str
def accepts_state_value(payload: Mapping[str, Any], key: str, value: object) -> bool
```

`strict=True` (producer) uses the 40 000-byte cap on canonical bytes. It adds these rules after all client rules,
so the first client error is unchanged: reject unreachable nodes (`nodes[i]`, `unknown_ref`), reject unknown node
types (`nodes[i].type`, `bad_type`), and reject the same `action_id` on two submit sources (`…action_id`,
`duplicate_id`). The client-valid `content.json` deliberately includes a reachable `future_widget` at `nodes[9]`;
client mode degrades it to its fallback, while producer strict mode rejects it at `nodes[9].type` with `bad_type`.
The earlier claim that all valid fixtures satisfied strict rules was incorrect; vendored fixtures remain unchanged.

```python
# fallback.py / content.py
def render_fallback_markdown(payload: Mapping[str, Any], *, status_line: str | None = None) -> str
def render_fallback_html(fallback_md: str) -> str  # T4-aware conversion, including no-Markdown path

def build_ui_content(payload: Mapping[str, Any], fallback_md: str, *, to_html: Callable[[str], str] | None = None,
                     sanitize_html: Callable[[str], str], thread_metadata: Mapping[str, Any] | None
                     ) -> dict[str, Any] | InteractiveUiError
def build_ui_edit(original_event_id: str, new_content: dict[str, Any], summary: str) -> dict[str, Any] | InteractiveUiError

# store.py — one instance per adapter, path pinned at connect (profile scope)
@dataclass
class UiRecord:
    ui_event_id: str; room_id: str; ui_id: str; session_key: str; thread_id: str | None
    revision: int; status: str; payload: dict[str, Any]; bot_mxid: str; recovered: bool = False
class InteractiveUiStore:
    def __init__(self, path: Path) -> None
    def get_by_event(self, ui_event_id: str) -> UiRecord | None
    def get_by_ui_id(self, ui_id: str) -> UiRecord | None
    def insert(self, record: UiRecord) -> None
    def commit_revision(self, ui_event_id: str, *, expected_revision: int, payload: dict[str, Any]) -> bool
    def accept_action(self, ui_event_id: str, *, nonce: str, action_event_id: str, sender: str,
                      expected_revision: int, single_use_payload: dict[str, Any] | None) -> AcceptOutcome
    # AcceptOutcome = Literal["accepted", "duplicate", "stale", "inactive"]; one BEGIN IMMEDIATE transaction
    # Missing record after duplicate checks: raises KeyError(ui_event_id); caller recovers/refuses without a turn.
    # Malformed replacement identity/revision/status: ValueError, with no mutation; stale CAS returns False.
    def prune(self, *, older_than_days: int = 90) -> int

# submission.py
@dataclass(frozen=True)
class SubmitSource:
    node_id: str; item_id: str | None; action_id: str; keys: frozenset[str]; single_use: bool; label: str
def submit_sources(parsed: ParsedUi) -> dict[str, SubmitSource]          # action_id → source (unique, strict)
def validate_action_values(parsed: ParsedUi, source: SubmitSource, values: object) -> list[str] | None
    # None = valid; else the invalid keys (exact key-set equality, accepts_state_value, required text)

# producer.py
class InteractiveUiController:
    def __init__(self, adapter: "MatrixAdapter", store: InteractiveUiStore) -> None
    async def present(self, *, chat_id: str, thread_metadata: dict | None, session_key: str,
                      ui: dict[str, Any], summary: str | None, replace_ui_id: str | None,
                      status: str | None) -> dict[str, Any]   # {"ok": True, ...} | {"ok": False, "error": str}

# tool.py
PRESENT_UI_SCHEMA: dict          # name "present_ui"; params: ui (object, required), summary?, replace_ui_id?, status?
def register_present_ui(ctx) -> None   # ctx.register_tool(name="present_ui", toolset=TOOLSET, ..., check_fn=matrix_deps_present)

# inbound.py
async def handle_action_event(adapter: "MatrixAdapter", *, room_id: str, sender: str, event_id: str,
                              event_ts: float, content: dict, relates_to: dict) -> None

# hermes_cli/plugins.py (generic seam, T2)
class PluginContext:
    def deliver_to_conversation(self, fn: Callable[[ConversationTarget], Awaitable[dict]],
                                *, timeout_s: float = 90.0) -> dict
# ConversationTarget(platform: str, chat_id: str, adapter: Any, metadata: dict | None, session_key: str)
# Resolves exactly like post_message (bound session key, live route, ownership, delivery adapter), refuses
# local sessions ("unsupported_surface") and calls on the gateway loop ("on_event_loop"), never raises.
```

**Tool input (`PRESENT_UI_SCHEMA`).** `ui` is the payload without `version`, `ui_id`, `revision` and `status`, which
Hermes fills in. It is a flat, non-recursive JSON Schema: `title`, `status_text`, `state`, `root`, and `nodes[]`,
where each node is an object with a `type` enum, `id`, and `additionalProperties: true`. The per-type catalog is
summarised compactly in the description. Validation error hints carry the details, so the schema stays small (target
≤ 2 500 tokens, measured in T6). `replace_ui_id` revises an earlier card from **this** conversation. `status`
(`active|expired|superseded`) is allowed only together with `replace_ui_id`. `submitted` is owned by Hermes. If the
model passes `version`, `ui_id`, `revision` or `status` inside `ui`, they are ignored and the result says so.
Images may carry a Hermes-only `upload: "<absolute local path>"` instead of `url`/`file` (T6, decision D-3). Hermes
replaces it with `url` or `file` before validation, so `upload` never reaches the wire.

**Tool result.** `{"ok": true, "ui_id", "revision", "event_id", "status", "content_bytes"}`, or
`tool_error("interactive_ui: <path>: <code>: <hint>")`. Other errors are `not_matrix_conversation`,
`unknown_ui_id`, `ui_in_other_conversation`, `send_failed: <reason>` and `delegated_child`. The description tells
the model to use a card only when interaction or structure helps, to keep labels meaningful, never to put secrets in
UI, that the card already contains its content (so the text reply should be at most one sentence), that a pressed
action arrives as a new user turn, and to answer in plain text if the tool errors twice.

### 2.4 Data flow

**Produce.**
1. The model calls `present_ui`. The handler, on the agent worker thread, refuses delegated children
   (`agent.delegation_context.is_delegated_child_context()`).
2. It calls `ctx.deliver_to_conversation(fn)`, which resolves the bound session to its delivery adapter. If the
   adapter is not a `MatrixAdapter` with a connected controller, the result is `not_matrix_conversation`.
3. On the gateway loop, `fn` calls `controller.present(...)` under `controller.lock`:
   - **New card:** generate `ui_id = "ui_" + token_urlsafe(12)`. **Revision:** load the record by `ui_id`. It must
     have the same `session_key` and room. `revision` becomes the stored revision + 1.
   - Resolve `upload` images (T6). Sanitise `rich_text.html`.
   - Run `parse_interactive_ui(full, strict=True)`. On error, stop with a tool error.
   - Run `render_fallback_markdown`, then `build_ui_content` or `build_ui_edit` (byte budgets). On error, stop.
   - Send the event. On success, store the record (`insert` / `commit_revision`) and return the ids.

   The send happens **before** the commit, with the lock held across both. A failed send then never advances the
   stored revision, and no action can be validated against a revision that never reached the room.

**Act.**
1. `_on_room_message` runs. Its first statement after the self-sender check skips `_try_secret_capture` when
   `INTERACTIVE_UI_ACTION_KEY` is in the content, so an action is never taken as a secret.
2. The existing guards run unchanged. Then, if the action key is present and `msgtype` is `m.text`, it calls
   `inbound.handle_action_event`. This happens before the text/media dispatch, so the action is never handled as
   ordinary text.
3. `handle_action_event` derives context with `_resolve_message_context(..., defer_effects=True)` and
   canonicalizes its full source before authorization. It calls the existing gateway
   `_is_user_authorized_for_source(source)` policy seam; callback-only adapters forward chat type, room and
   thread through `_is_sender_authorized`, and unwired standalone adapters retain `_is_authorized_user`.
   Unauthorized actions drop silently before thread participation, read receipts, notices, acceptance or edits.
   Authorized handling binds the identity's runtime home via `_profile_runtime_scope(..., hydrate_secrets=False)`
   for confirmation, notices and dispatch, while the adapter store stays pinned. It then parses the action object
   (version, field types, nonce pattern). A malformed action gets the `bad_action` notice.
4. It loads the record, or recovers it from the room (T7). If none, `unknown_ui` notice. It then checks:
   - the room is the same and the target is the original event id;
   - the session key is the same, otherwise the `other_conversation` notice;
   - `ui_id`, the source, and `validate_action_values` pass.
5. `store.accept_action(...)` runs, and its outcome decides the next step:
   - `duplicate`: silent drop.
   - `stale` or `inactive`: notice.
   - `accepted` with `single_use`: send the status edit (one retry, and a warning if it fails).
6. `handle_message(MessageEvent(text=<server-built action text>, source=source, message_id=event_id,
   reply_to_message_id=ui_event_id, reply_to_text=<title>, message_type=TEXT, raw_message=content))`.

The server-built turn text, for example:

```
[Interactive UI action] The user pressed “Explain this mechanism” on card ui_3fQ… (“Book knowledge map”, revision 2).
action_id: explain_node
values:
- focus = "maillard" (Maillard reaction)
To update this card, call present_ui with replace_ui_id="ui_3fQ…".
```

### 2.5 Security and profile scope

- **Trust.** Model output and room content are untrusted. Hermes validates the payload before sending, and the
  client validates it again. An action is trusted only against a record that Hermes wrote (or rebuilt from events
  sent by its own `mxid`), for the same room and conversation, at the latest revision, with values the user could
  have chosen. The reply's `body` and `values` never name tools or prompts. Their only path to the model is the
  quoted value list in the turn text, and they are user input already.
- **Unauthorised senders.** They get silence and a DEBUG log. This deliberately departs from Web plan §2.4, where
  they would get a notice: Hermes should not act as an oracle for unpaired users. Notices are sent only to authorised
  users.
- **Profile scope.**
  - One adapter is built per served profile. The store path comes from the adapter's `_store_dir`, which is pinned
    at `connect()` inside the profile scope.
  - No module globals, no `os.environ` reads, and no `get_hermes_home()` at call time.
  - Database work runs on the adapter's loop or through `asyncio.to_thread`, which copies the context.
  - The tool path inherits the turn's scope and resolves its adapter through `_delivery_adapter_for` and the
    ownership check.
  - The session-key equality check (criterion 9) blocks cross-profile, cross-thread and cross-user injection.
- **Media.** `upload` paths must be absolute regular files under the size cap, and the magic bytes must be
  PNG, JPEG, GIF or WebP; anything else is rejected as `bad_value`, so `upload` can never publish a non-image file.
  In encrypted rooms, the upload uses the same encryption the adapter uses for media.
- **Logs.** Logs carry event ids, the `ui_id`, the revision, error codes and byte sizes. They never carry payload
  text, values or nonces.

### 2.6 Persistence, replay and concurrency

- SQLite at `<_store_dir>/interactive_ui.sqlite3` with WAL. Tables:
  - `uis(ui_event_id PK, room_id, ui_id UNIQUE, session_key, thread_id, bot_mxid, revision, status, payload_json,
    recovered, created_at, updated_at)`
  - `nonces(ui_event_id, nonce, action_event_id UNIQUE, sender, accepted_at, PRIMARY KEY(ui_event_id, nonce))`

  `prune(90 days)` runs on connect and removes only mutable UI rows. Accepted repeatable actions refresh
  `updated_at`; nonce/event tombstones reference ownership receipts and persist for their full lifetime.
  Ownership receipts also retain authoritative `latest_revision`/`latest_status` watermarks, updated in the
  same transaction as insertion, model revision or single-use acceptance, independently of mutable rows.
- `accept_action` runs one `BEGIN IMMEDIATE` transaction. It checks the nonce, revision and status, inserts the
  nonce and, for `single_use`, writes `status = submitted`, `revision + 1` and the new payload. The commit **precedes**
  the status edit, so durable dedupe comes before any visible effect. A crash after the commit but before the turn
  loses that turn, and a retry then gets the `inactive` notice. This is a documented, accepted window, since inbound
  turns are not durable anywhere in Hermes.
- `controller.lock` (`asyncio.Lock`) serialises `present`, `accept_action` and the status edit for one adapter. This
  makes concurrent deliveries in one `asyncio.gather` sync batch deterministic: the first nonce wins, and a second
  single-use action sees `inactive`.
- Replay: in-memory `_is_duplicate_event` and startup grace first, then the durable nonce check. The same nonce sent
  in a new event is caught by the `(ui_event_id, nonce)` key. Re-delivery of the same event is caught by
  `action_event_id UNIQUE`.
- Recovery when the record is missing (subtle, T7):
  1. `client.get_event(room_id, ui_event_id)`, decrypting with `client.crypto` if it is encrypted.
  2. Require the sender to equal the adapter's own `mxid`, the content to carry the payload, no `m.replace`
     relation, and not redacted.
  3. Fetch the `m.replace` relations (`/_matrix/client/v1/rooms/{room}/relations/{event}/m.replace`, paginated, at
     most 50), decrypt them, keep only the bot-authored ones, and take the highest valid `revision`.
  4. Recover only when an independent durable ownership receipt for the original event already establishes
     `room_id`, `ui_id`, `session_key`, `thread_id`, and `bot_mxid`. Match that receipt against fetched content and
     the acting source exactly; never assign ownership from the acting user or current thread-root routing.
     Require a trusted durable revision/status watermark: refuse room state older than it, a different status at
     the same revision, or reactivation of a submitted/expired/superseded card. Store the result with `recovered = 1`
     and the receipt's ownership, advancing the watermark atomically. Without a receipt or provable watermark,
     refuse with `unknown_ui` and no turn. Complete database loss cannot be recovered safely from room events alone.

  The exact mautrix 0.21.1 calls are confirmed in T7.

### 2.7 Configuration and setup UX

- **One switch.** Enable the `interactive_ui` toolset for the Matrix platform with `hermes tools` → Matrix, or with
  `platform_toolsets.matrix` in `config.yaml`. There is no new environment variable and no new non-secret key.
- **Setup prompt.** The Matrix `interactive_setup` asks "Enable interactive UI replies (Element Web labs flag
  `feature_interactive_ui`)? [y/N]" and saves the choice through the existing tools-config save helper. The prompt
  moves to `plugins/platforms/matrix/setup.py` so that `adapter.py` does not grow.
- **Inbound always on.** Inbound handling works whether or not the toolset is enabled, because actions only matter
  for UIs this bot sent.
- **Docs.** `website/docs/user-guide/messaging/matrix.md` gets an "Interactive UI" section covering enablement,
  client support, the fallback, the action and confirmation semantics, and the storage location.

### 2.8 Conventions

- Facade plus siblings, with late imports of the facade inside functions.
- Size limits per unit: cyclomatic complexity ≤ 20, ≤ 300 lines, nesting ≤ 6. Use tables for node-type dispatch
  (in the fallback renderer and the hints).
- No defensive `try/except: pass`.
- Comments explain why, not what.
- `display_hermes_home()` for any path shown in the schema.
- Tests go through `scripts/run_tests.sh` and use real imports against a temporary `HERMES_HOME`.
- No change-detector or source-reading tests.

### 2.9 Known parity hazards in the Python port (T1, subtle)

JS `Object.keys` order (integer-like keys first) is replicated for first-error order. JS `String.prototype.trim`
whitespace set is replicated (Python `strip` differs on `\x85`, `﻿` and others). The other hazards:

- `Number.isSafeInteger`: `bool` is never a number, and an integral `float` counts as an integer.
- `NaN`/`Infinity` are rejected.
- Code-point lengths use Python `len`.
- Schema regex patterns are checked for Python `re` compatibility.
- URL checks (`https`, a host, no userinfo), using public `idna` UTS46 APIs. This conservatively rejects some
  rare Unicode hostnames accepted by Node's WHATWG URL parser; general URL parity is not exact (see review log).
- Byte counting of the client copy uses JS number formatting.

The Web inconsistency (parsed strings are trimmed before the length check, while saved or submitted strings use raw
length) is recorded here, not fixed. The producer applies the trimmed rule. Inbound `values` use the same raw-length
rule as Web `validateSubmission` (`accepts_state_value` on the raw value), so Hermes accepts exactly what a
conforming client sends.

### 2.10 Decisions (user, 2026-11-03)

- **D-1 Licence of vendored files: dual licence.** The schema and fixtures are
  `AGPL-3.0-only OR LicenseRef-Element-Commercial`, © 2026 sinkerine, and the Python parser is a port of
  `protocol.ts`. As copyright holder, the user also licenses the copies vendored into Hermes and the Python port as
  MIT. The vendor README records the source commit `bcb080171cb312feef37e63007c7ca8cc0a8b77f`, the original SPDX
  line and that MIT grant. `protocol.py` carries the same notice in its header.
- **D-2 E2E gate.** Add an "E2E sandboxes" section to `tests/AGENTS.md`: an isolated
  `HERMES_HOME`/`HERMES_RUNTIME_DIR` under the ignored `.hermes-sandbox/`, disposable containers and accounts, no
  real `~/.hermes`, users or rooms, and a pointer to `scripts/sandbox/matrix_interactive_ui/README.md`. Mention
  "E2E sandboxes" in the root routing row for `tests/`, trimming wording elsewhere so the root stays at or under
  12 000 characters (`python scripts/check`).
- **D-3 Image upload: yes, local files only.** Absolute regular files, magic bytes PNG/JPEG/GIF/WebP, size cap,
  encrypted upload in E2EE rooms. No https fetch in H1.
- **D-4 Generic seam:** `PluginContext.deliver_to_conversation`. The plugin does not import gateway internals.
- **D-5 Over-cap:** a strict tool error. `formatted_body` is never dropped to make an event fit.

## 3. Tasks

Unit test environment for every task (build once, see §4): `scripts/run_tests.sh <paths>`.

### Wave 1 (parallel: disjoint files, no dependencies)

**T1 — Protocol port, vendored contract, conformance** *(subtle: exact first-error parity, fuzz safety)*
- Goal: a Python parser equivalent to `parseInteractiveUi` plus the producer strict mode, using the vendored schema
  as the interpreter's rule table.
- Owns:
  - `plugins/platforms/matrix/interactive_ui/{__init__.py,protocol.py}`
  - `plugins/platforms/matrix/interactive_ui/vendor/element-web-bcb0801/{interactive-ui-v1.schema.json,fixtures/**,README.md}`
  - the `pyproject.toml` package-data line and explicit bounded `idna` dependency in the Matrix extra; `uv.lock`
    regenerated through `hermes pm lock`
  - `tests/plugins/platforms/matrix/interactive_ui/{__init__.py,test_protocol_conformance.py,test_protocol_strict.py,test_protocol_fuzz.py}`
- Produces: the §2.3 protocol interfaces and constants.
- Consumes: nothing new.
- Tests:
  - every fixture in both modes;
  - strict rules;
  - 40 000 / 60 000 byte boundaries and 300 nodes in under 200 ms;
  - seeded fuzz;
  - JS-parity edge cases from §2.9;
  - `accepts_state_value` rulings (null string/number inferred; null bool/array rejected; contradictory bindings;
    graph-union selection; scatter 400 points).
- Command: `scripts/run_tests.sh tests/plugins/platforms/matrix/interactive_ui/`

**T2 — Generic seams: conversation delivery and toolset restriction**
- Goal: let an in-process plugin tool run adapter work for its own conversation, and make `interactive_ui` an
  off-by-default, Matrix-only plugin toolset.
- Owns:
  - `hermes_cli/plugins.py` (new method plus minimal extraction of existing messaging implementation to keep the
    facade at or below its base length; retain public method signatures) and `hermes_cli/plugin_messages.py`
  - `plugin_isolation.HOST_UNSUPPORTED_CTX_METHODS`
  - `hermes_cli/toolset_scope.py` and `hermes_cli/tools_config.py`
  - `website/docs/developer-guide/plugins/index.md` (`deliver_to_conversation` paragraph)
  - `tests/hermes_cli/test_plugin_deliver_to_conversation.py` and `tests/hermes_cli/test_plugin_toolset_platform_restriction.py`
- Produces: `PluginContext.deliver_to_conversation`, `ConversationTarget`, and restricted, default-off plugin
  toolsets.
- Tests:
  - A bound session with a fake runner and adapter resolves to `_delivery_adapter_for`.
  - There are refusals for no session, a local session, being on the loop, another profile's session and a timeout.
  - A frozen test plugin loaded through real discovery gets its toolset only on `matrix`, and only when explicitly
    enabled.
  - The restriction fix applies to plugin toolsets (red on base).
- Command: `scripts/run_tests.sh tests/hermes_cli/test_plugin_deliver_to_conversation.py tests/hermes_cli/test_plugin_toolset_platform_restriction.py tests/hermes_cli/test_tools_config.py`

**T3 — Extract Matrix HTML/Markdown helpers (behaviour-preserving)**
- Goal: free `adapter.py` lines and expose `markdown_to_matrix_html(text)` / `sanitize_matrix_html(html)` as pure
  functions.
- Owns:
  - `plugins/platforms/matrix/adapter_html.py` (new)
  - `plugins/platforms/matrix/adapter.py`, removing `_MatrixHtmlSanitizer`, `_sanitize_matrix_html`,
    `_pre_sanitize_matrix_markdown`, the LaTeX token helpers and `_markdown_to_html_fallback`, and turning
    `_markdown_to_html` into a one-line delegate
  - any test that imports the moved names (no re-export shims; tests are updated)
- Produces: `adapter_html.markdown_to_matrix_html`, `adapter_html.sanitize_matrix_html`, and at least 150 lines of
  headroom in `adapter.py`.
- Tests: the existing Matrix rendering, LaTeX and Markdown suites unchanged in behaviour.
- Command: `scripts/run_tests.sh tests/gateway/test_matrix.py tests/gateway/test_matrix_latex_maths.py tests/gateway/test_matrix_message_length.py`

### Wave 2 (parallel: disjoint files)

**T4 — Fallback renderer and event content builders** *(subtle: completeness, determinism, byte budgets)*
- Owns: `interactive_ui/{fallback.py,content.py}` and
  `tests/plugins/platforms/matrix/interactive_ui/{test_fallback.py,test_content.py}`.
- Produces: `render_fallback_markdown`, `build_ui_content` and `build_ui_edit`.
- Depends on: T1 and T3.
- Rendering is table-driven per node type:
  - Tables become one bullet line per row (`**first cell** — Col: v · Col: v`), because Element X collapses
    Markdown tables.
  - Charts become their data per category or point.
  - Graphs list their layers, then nodes with details, then `A → B (label)` edges.
  - Controls list their options, with the current value marked.
  - Conditional branches are labelled, for example `When “Branch” is “Proteins”:`.
  - Actions are listed as `▸ label`.
- Tests:
  - a completeness walk over every valid fixture;
  - byte-identical output for identical input;
  - Chinese and English knowledge maps;
  - the 44 000 / 45 000 boundaries producing `too_large` at path `""`;
  - edits carrying no top-level payload and `m.new_content` without `m.relates_to`;
  - the thread relation kept on new events;
  - `"m.mentions": {}`.
- Command: `scripts/run_tests.sh tests/plugins/platforms/matrix/interactive_ui/test_fallback.py tests/plugins/platforms/matrix/interactive_ui/test_content.py`

**T5 — Durable store and server-side submission validation** *(subtle: atomic acceptance, restart semantics)*
- Owns: `interactive_ui/{store.py,submission.py}` and
  `tests/plugins/platforms/matrix/interactive_ui/{test_store.py,test_submission.py}`.
- Produces: `InteractiveUiStore`, `UiRecord`, `AcceptOutcome`, `submit_sources` and `validate_action_values`.
- Depends on: T1.
- Tests:
  - accepted, duplicate, stale and inactive outcomes;
  - a thread-pool race of 20 identical nonces with exactly one accepted;
  - a fresh store instance on the same file (restart);
  - prune;
  - single-use transitions;
  - exact key sets, extra keys, missing keys, wrong types, out-of-option values, required empty text and graph
    selection;
  - the form default `single_use: true` and the button default `false`;
  - validation of carousel item submits.
- Command: `scripts/run_tests.sh tests/plugins/platforms/matrix/interactive_ui/test_store.py tests/plugins/platforms/matrix/interactive_ui/test_submission.py`

### Wave 3

**T6 — Producer: tool, controller, media, adapter wiring, setup and docs** *(subtle: send/commit ordering, gating)*
- Owns:
  - `interactive_ui/{producer.py,tool.py,media.py}`
  - `plugins/platforms/matrix/setup.py` (moved `interactive_setup` plus the new prompt)
  - `plugins/platforms/matrix/adapter.py`: controller and store construction in `connect()` after `_store_dir` is
    pinned, closing in `disconnect()`, and `register_present_ui(ctx)` in `register()`; net lines ≤ base
  - `website/docs/user-guide/messaging/matrix.md`
  - `tests/plugins/platforms/matrix/interactive_ui/{test_tool.py,test_producer.py,test_media.py}`
  - `tests/gateway/test_matrix_plugin_setup.py` (extended)
- Produces: the `present_ui` tool in toolset `interactive_ui`, and `InteractiveUiController.present`.
- Depends on: T2, T4 and T5.
- Tests:
  - Through the real registry (`registry.dispatch`): no bound session → `not_matrix_conversation`; delegated
    child → refused; a valid knowledge map → one `send_message_event` call on the fake client with the exact §2.3
    shape and a stored record.
  - Invalid payload → a path-specific error and no send.
  - Revision → `m.replace` and revision + 1. A failed send keeps the revision. Another conversation's `ui_id` →
    refused.
  - `upload`: PNG and JPEG accepted; a text file renamed `.png` rejected; the encrypted-room path yields `file`.
  - The schema token budget is measured and recorded, not asserted as a snapshot.
  - The tool is absent from a telegram platform toolset and present on matrix only when enabled.
- Command: `scripts/run_tests.sh tests/plugins/platforms/matrix/interactive_ui/ tests/gateway/test_matrix_plugin_setup.py`

### Wave 4

**T7 — Inbound action pipeline** *(subtle: guard order, dedupe, immediate confirmation, recovery, scope)*
- Owns:
  - `interactive_ui/inbound.py`
  - `plugins/platforms/matrix/adapter.py`: the two `_on_room_message` hooks only; net lines ≤ base
  - `locales/*.yaml` (`platform.matrix.interactive_ui.{submitted_status,stale,inactive,unknown,invalid,other_conversation}`)
  - `tests/plugins/platforms/matrix/interactive_ui/{test_inbound.py,test_inbound_recovery.py,test_inbound_profiles.py}`
- Consumes: T5 and T6.
- Tests (real `MatrixAdapter` with a fake mautrix client and recorded `handle_message`):
  - unauthorised → nothing;
  - bridge sender or ignored pattern → nothing;
  - a pending secret capture is not consumed by an action;
  - single use: the edit is sent before `handle_message` (ordering asserted), with exactly one turn;
  - repeatable: no edit, one turn;
  - duplicate nonce and replayed event → no turn, no notice;
  - stale and inactive → one notice each, no turn;
  - spoofs (edit id as target, another room, a non-bot-authored target, another conversation's session key, an
    unknown `action_id`, extra or invalid values) → rejected;
  - concurrent `gather` of two actions → one turn;
  - restart with a new adapter on the same store → duplicate rejected;
  - missing record → room fetch with a decrypted event and edits → accepted, while a forged edit from another
    sender is ignored;
  - two profiles' adapters with separate stores cannot act on each other's UIs;
  - thread relation preserved on the turn's source.
- Command: `scripts/run_tests.sh tests/plugins/platforms/matrix/interactive_ui/ tests/gateway/test_matrix.py tests/gateway/test_matrix_secret_capture.py tests/agent/test_i18n.py`

### Wave 5

**T8 — E2E sandbox harness, model eval and repository instructions**
- Owns:
  - `scripts/sandbox/matrix_interactive_ui/{README.md,up.sh,down.sh,bootstrap.py,replay_action.py,fake_openai.py,eval_prompts.yaml}`
  - `tests/AGENTS.md` (E2E sandboxes section)
  - the root `AGENTS.md` routing-row wording (only if D-2 is approved)
- Produces: the runnable §5 journey and an evidence layout.
- Depends on: T6 and T7.
- Tests: `bash -n` and `python -I -m py_compile` on the harness; then the §5 journey itself.
- Command: `bash -n scripts/sandbox/matrix_interactive_ui/*.sh && python -I -m py_compile scripts/sandbox/matrix_interactive_ui/*.py`

## 4. Test and verification (whole feature)

Environment (from the worktree; never the real `~/.hermes`):

```bash
cd /home/sinkerine/.paseo/worktrees/15zkmyhp/matrix-interactive-ui
export SB="$PWD/.hermes-sandbox/matrix-ui"            # .hermes-sandbox/ is git-ignored
export HERMES_HOME="$SB/home" HERMES_RUNTIME_DIR="$SB/home/tools"
mkdir -p "$HERMES_HOME"
source ./activate                                     # PM-provisioned environment
python -m pm.build_env --source . --out .venv --group dev --group test --extra matrix   # fresh build; remove a stale .venv first
```

Checks, all run after the last edit:

```bash
scripts/run_tests.sh tests/plugins/platforms/matrix/ tests/gateway/ -k "matrix or interactive"   # feature + Matrix regressions
scripts/run_tests.sh tests/hermes_cli/test_plugin_deliver_to_conversation.py tests/hermes_cli/test_plugin_toolset_platform_restriction.py tests/hermes_cli/test_tools_config.py tests/agent/test_i18n.py
scripts/run_tests.sh tests/plugins/ tests/tools/          # plugin discovery + registry contracts
scripts/run_tests.sh                                       # full suite; unrelated pre-existing failures are reported, not claimed
python scripts/check                                       # CI lint, code-health ratchet (adapter.py ≤ base), types
python -I -c "import importlib.resources as r; assert r.files('plugins.platforms.matrix.interactive_ui').joinpath('vendor/element-web-bcb0801/interactive-ui-v1.schema.json').is_file()"
```

Build check: T1 also builds a sealed environment through PM, `python -m pm.build_env --source . --out "$SB/sealed"
--sealed`, and checks that the vendored schema is inside it. If the PM flags differ, record the command actually
used.

## 5. E2E journey

### Sandbox (all under `$SB`, loopback-bound, disposable accounts; T8 scripts)

**Bring up** (`up.sh`):
1. Check ports 18008, 18081 and 18082 are free (`ss -ltn`). Port 8080 is in use by an unrelated service, and the
   sandbox leaves it alone.
2. Start Synapse:
   ```bash
   docker run --rm -e SYNAPSE_SERVER_NAME=localhost -e SYNAPSE_REPORT_STATS=no -v "$SB/synapse:/data" \
     matrixdotorg/synapse:latest generate
   ```
   Keep open registration off and set `registration_shared_secret`, then start `hermes-mui-synapse` on
   `127.0.0.1:18008`.
3. Run `bootstrap.py`. It registers `@hermes-bot`, `@alice` and `@bob` with random passwords kept in
   `$SB/accounts.json` (mode 0600, never printed). It logs in the bot and writes the bot token to
   `$HERMES_HOME/.env` with `MATRIX_HOMESERVER` and `MATRIX_E2EE_MODE=required`.
4. Write the sandbox config `$HERMES_HOME/config.yaml`:
   - `platform_toolsets.matrix` with `interactive_ui` enabled;
   - `matrix.allowed_users: ["@alice:localhost", "@bob:localhost"]`;
   - model `provider: custom`, `base_url: https://magpie-api.sachi.m.mado.moe/v1`, `default: gpt-6.1-sol`,
     `key_env: MAGPIE_API_KEY`.

   `MAGPIE_API_KEY` is copied into the sandbox `.env` from the Magpie provider entry in `~/.pi/agent/models.json`
   without echoing it. It is a placeholder key on a LAN-only endpoint, per `/nixfiles`. No production config is
   copied.
5. Build Element Web at `bcb0801` without touching the reference checkout. Its `webapp/` build is from 2026-10-20 and
   predates the feature.
   ```bash
   git -C /home/sinkerine/dev/element-web archive bcb0801 | tar -x -C "$SB/element-src"
   pnpm install --frozen-lockfile
   (cd apps/web && pnpm build)
   ```
   Write `config.json` for `http://127.0.0.1:18008` with `show_labs_settings: true` and
   `features.feature_interactive_ui: true`, then serve `webapp/` on `127.0.0.1:18081`.
6. Start the gateway:
   ```bash
   scripts/dev-sandbox.sh --persistent ...   # or: hermes gateway run under the exported HERMES_HOME
   ```
   Log to `$SB/evidence/gateway.log`.

**Tear down** (`down.sh`): stop the gateway, the static server and `hermes-mui-synapse`. Keep `$SB/evidence/`, and
remove everything else in `$SB` only with `--purge`. No production gateway, real room, real user or personal device
is ever involved.

**Browser.** Use the Paseo browser tools (navigate, type, click, resize, screenshot) against `127.0.0.1:18081`, at
1280 px and 360 px widths. Screenshots go to `$SB/evidence/`.

### Steps

| # | User step | Expected result | Evidence |
|---|---|---|---|
| 1 | Alice logs in to Element (flag on) and creates an **encrypted** DM with `@hermes-bot`. | The room is E2EE and the bot joins. | screenshot, gateway log |
| 2 | Alice asks (Chinese): “用知识图谱解释《风味的法则》里成分—机制—结果的关系，并允许按知识分支切换”. | GPT calls `present_ui` once (or self-corrects once). One UI event arrives. The card shows a heading, a branch select and a layered graph. No fallback text is visible. | screenshots at 1280 and 360 px, log line with `ui_id`/revision/bytes |
| 3 | Switch the branch and select a mechanism node. | Local change only. The gateway receives no event. | log shows no inbound |
| 4 | Press the repeatable “Explain this mechanism”. | A visible reply appears. Hermes logs acceptance with no edit. Exactly one turn follows, and the answer addresses the selected node. | log, screenshot |
| 5 | Repeat step 2 in English. Then ask for a comparison table, a bar or line chart, and a short form (“plan my week”). | The cards render, the table sorts, and the chart data view works. | screenshots |
| 6 | Submit the form (single use). | The card switches to “submitted” with the status text within 8 s, before the model's reply. The model replies once. | timestamps in the log (edit before turn dispatch), screenshot |
| 7 | Press submit again (the client's “Try again” path). | A short `inactive` notice and no new turn. | log, screenshot |
| 8 | `replay_action.py` re-sends the step 6 action content (same nonce), then one with an old revision. The script runs as Alice with its own E2EE device in the same room. If its device cannot be verified for decryption, it uses an unencrypted sibling room, and that limitation is recorded. | Duplicate: silence, no turn. Stale: one notice, no turn. | log |
| 9 | Restart the gateway and repeat step 8's duplicate. | Still rejected (durable store). | log |
| 10 | Bob logs in with the labs flag **off** and opens a room where the bot sent the cards. | Every card shows a complete, readable fallback (all nodes, edges, rows, options, actions). | screenshot |
| 11 | Ask for a carousel with images. The model downloads or generates 3 images to local files and passes `upload` paths. | Encrypted images decrypt and display. “Item n of m” appears. | screenshot |
| 12 | Prompt-set eval (`eval_prompts.yaml`, bounded to ≤ 30 model requests): zh/en knowledge maps, table, chart, form, carousel, a forced invalid payload, and two prompts where UI is **not** useful (a factual one-liner and a joke). | Recorded per prompt: whether the tool was used, retries, validation errors, and whether UI was used appropriately. This is a quality report, not a pass/fail gate, except that the invalid-payload prompt must self-correct. | `$SB/evidence/eval.json` |
| 13 | Deterministic run with `fake_openai.py` as the provider: scripted invalid → corrected `present_ui`, then an action turn answered by text. | The same assertions as steps 2, 4, 6 and 8, with no model variance. | log |

**Honest blockers.**
- Magpie must be reachable from this host (a LAN-only endpoint). If it is not, steps 2–12 run on `fake_openai.py`
  only, and real GPT quality is reported as unverified.
- The Element Web build needs `pnpm` and network access to install dependencies. If that fails, browser steps are
  blocked, and only bot-side and protocol evidence is reported. No mocks are presented as screenshots.
- Element X Android (A1) is out of scope.

## 6. Review log

- 2026-11-03: the user directed GPT evaluation to the Magpie provider from `/nixfiles` (OpenAI-compatible,
  `gpt-6.1-sol` / `gpt-6-luna`).
- 2026-11-03: the user decided D-1 to D-5: MIT grant for the vendored contract and port; the E2E section in
  `tests/AGENTS.md` plus the root routing row; local image uploads; `deliver_to_conversation`; a strict over-cap
  error.
- 2026-11-03: the user approved the plan.
- Ruling: preserve client acceptance and strict producer rejection of the deliberate unknown node in
  `valid/content.json` — verified against the canonical fixture; the all-valid-fixtures-pass-strict claim was
  incorrect — cost if wrong: producer rejects a future node until its supported catalog is extended. The client
  protocol and vendored fixture remain unchanged.
- Ruling: use the public `idna` UTS46 APIs and declare the dependency directly in the Matrix extra, with a tested
  floor and `<4` upper bound — Python's built-in IDNA2003 differs from browser URL validation, and a direct
  runtime import should not rely on transitive dependencies — cost if wrong: an extra declared dependency and
  lock refresh. Add regression cases for the observed Unicode hostname differences; do not claim full WHATWG
  parity without evidence.
- Ruling: retain conservative rejection for rare Unicode hostname discrepancies after UTS46 mapping — a seeded
  1 500-hostname probe found eight differences involving bidi checks or Unicode-version tables, and expanding to
  a dedicated WHATWG engine is disproportionate to this feature — cost if wrong: some client-valid Unicode URLs
  are refused with a tool error. This is an explicit compatibility limitation in both parser modes, not a claim of
  exact URL parity; canonical fixture and first-error conformance remain required. T1 must preserve probe details
  and representative regression cases for review.
- Ruling: permit minimal extraction of existing `PluginContext` messaging behavior into its messaging sibling
  while preserving public methods — T2's new delegate exceeds the existing facade size ratchet; removing unrelated
  blank lines only to satisfy the count would obscure the structural issue — cost if wrong: a small additional
  refactor requiring existing messaging regression checks. Prefer a concise delegate if that alone fits.
- Ruling: preserve the explicit `all` toolset override as the exception already allowed by acceptance criterion 7 —
  changing global override semantics is outside T2 — cost if wrong: a non-Matrix model using `all` may see the
  schema but must receive a no-send refusal from T6. Ordinary named platform enablement must exclude restricted
  schemas; T6 must test actual dispatch refusal. Tests and review reports must not describe `all` passthrough as
  runtime schema exclusion. Fix the separately identified callback `TimeoutError` misclassification within T2.
- T4 environment incident: the worker reported an earlier command skipped sandbox setup after an inspection
  failure, and the test runner provisioned tools under `/home/sinkerine/.hermes`. This violated the worktree-only
  constraint. No external cleanup was attempted. Final worker validation and the parent's independent 345-test
  rerun used the sandbox interpreter. Subsequent validation must use `set -e`, then source the sandbox environment
  and assert the exact worktree-local `HERMES_HOME`, `HERMES_RUNTIME_DIR` and interpreter before any runner or PM
  command. The report does not establish the precise external file delta; preserve the incident for delivery.
- Ruling: add a T4-owned `render_fallback_html` and make `build_ui_content(to_html=...)` optional, defaulting to
  that converter — the shared renderer's no-Markdown fallback corrupts T4-generated escapes, but an explicit
  caller-supplied converter must retain precedence — cost if wrong: another narrow conversion path to maintain.
  Preserve shared adapter rendering; final sanitization and byte budgets still apply. T6 production wiring must
  use the new default or explicitly the T4 converter. Test missing-Markdown literal fidelity and custom-converter
  precedence, rather than detecting dependencies inside the builder and silently ignoring the supplied callable.
- Ruling: `accept_action` raises `KeyError(ui_event_id)` for a missing record after durable duplicate checks;
  replacement payloads with another `ui_id`, a revision other than `expected_revision + 1`, or a non-submitted
  single-use status raise `ValueError` without mutation — missing storage state and malformed caller transitions
  must not masquerade as accepted/stale actions — cost if wrong: T7 needs a specific missing-record handler that
  recovers or refuses without dispatching a turn. Optimistic revision mismatch still returns `False`.
- Ruling: expand T7 minimally to add immutable ownership receipts in the existing profile-pinned SQLite store,
  populated atomically on trusted insertion and backfilled only from existing trusted UI rows — room events lack
  historical profile/per-user ownership, so deriving a missing record's session from the acting user is unsafe —
  cost if wrong: additional retained metadata and recovery unavailable after complete database loss. Recovery
  requires a matching receipt and room/session/thread/bot identity; receipts survive UI pruning for recovery.
  No receipt means `unknown_ui`, no turn. Tests cover deletion with receipt, absent receipt, changed routing and
  cross-profile attempts; source/store tests and user docs are included in T7 ownership.
- Ruling: replay tombstones must survive for the full ownership/recovery lifetime, independently of mutable UI
  rows. Old pruning could remove nonce history even while a UI survived; therefore migration marks every
  pre-migration ownership receipt `replay_safe = 0`, preserves surviving history, and refuses actions/recovery
  for those cards — historical completeness cannot be proven — cost if wrong: existing local feature cards
  require a fresh `present_ui` card. Revisions and recovery never upgrade safety; only a genuinely new UI/event
  creates a safe receipt. Migration must be atomic, idempotent, versioned, and tested across reopen.
- T7 review fix: SQLite schema version 1 atomically migrates nonces from mutable `uis` foreign keys to
  lifetime `ownership` receipts, preserves all surviving acceptance rows, and marks every pre-migration
  receipt `replay_safe = 0`. Old pruning also removed aged nonces for surviving cards, so neither a legacy
  UI row nor its receipt proves complete history. Unsafe receipts refuse actions/recovery; revision,
  insertion and recovery cannot upgrade them. A genuinely new original card receives `replay_safe = 1`.
  Complete receipt/history loss remains unrecoverable. Tests cover migration rollback, repeated reopen,
  surviving/pruned legacy cards, acceptance activity refresh, delete/prune recovery and nonce/event replay.
- T7 round-two consumed-state recovery fix: schema version 2 adds authoritative revision/status watermarks to
  lifetime ownership receipts, atomically maintained by insertion, `commit_revision` and single-use acceptance.
  Recovery fails closed on room rollback or reactivation of terminal status, including when both confirmation
  sends failed before a successfully dispatched turn. This is distinct from the accepted commit-to-dispatch
  crash window. Version 1 safe receipts backfill only from matching trusted local UI rows; missing/unprovable
  state becomes unsafe. Version 0 and already-unsafe receipts never become safe. Migration DDL, backfill, safety
  changes and version stamp commit or roll back together; retained dedupe is preserved across repeated reopen.
