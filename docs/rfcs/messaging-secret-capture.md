# Secure Secret Capture over Messaging Gateways

**Status:** Implemented (Matrix first implementation)

**Primary consumer:** skill `setup.collect_secrets` on Matrix and other messaging gateways

**Problem:** Messaging sessions currently refuse secrets even when user explicitly asks Hermes to persist one. Interactive CLI/TUI/Desktop already have secure `secret.request` capture, but messaging gateways return an unsupported hint instead.

## Summary

Add platform-neutral secret capture to messaging gateways. Secret value is consumed by gateway before normal message dispatch, never sent to model, never persisted in session history, never included in tool arguments/results, and never logged. Platform adapter removes source message where supported, then existing `save_env_value_secure()` persists value into active profile's protected `.env`.

First implementation targets Matrix messages. Broader platform support can follow through same base interface.

This is not blanket permission for model to copy arbitrary chat text into secret storage. Capture must start from structured setup metadata and create one pending secret-entry request. The next eligible inbound text response is consumed as the value.

## Existing Architecture

Hermes already has most required pieces:

- Skills declare secrets through `setup.collect_secrets` and `required_environment_variables` (`tools/skills_tool.py`).
- Interactive surfaces register secret callbacks and persist through `save_env_value_secure()` (`tui_gateway/server.py`).
- `save_env_value_secure()` routes through unified credential lifecycle and profile-aware `.env` handling (`hermes_cli/config.py`, `hermes_cli/credential_lifecycle.py`).
- Gateway clarify prompts already implement register → notify → block → resolve → timeout with per-session state (`tools/clarify_gateway.py`).
- Matrix already implements sender-bound reaction approvals, expiry, prompt cleanup, and redaction (`plugins/platforms/matrix/adapter.py`).
- Gateway has pre-agent interception for clarify responses (`gateway/run.py`) and a turn-scoped
  structured secret callback wired through the current delivery adapter.

Current block is explicit:

- `tools/skills_tool.py` short-circuits non-interactive gateway surfaces before invoking secret callback.
- `gateway/platforms/base.py` says secure secret entry is unsupported over messaging.
- No messaging secret registry or pre-dispatch secret-response interception exists.

## Goals

1. Let a user satisfy `setup.collect_secrets` from a supported messaging platform.
2. Keep raw secret outside model context, session DB, tool transcript, logs, notifications, and error text.
3. Persist through existing secure/profile-aware credential lifecycle.
4. Use one secret-entry prompt with no separate consent step.
5. Reuse gateway prompt architecture; add no model-facing core tool.
6. Preserve prompt caching and message-role alternation.
7. Route the response to the pending capture without exposing the value to the model.
8. Fail closed on ambiguity or persistence failure.

## Non-Goals

- Accepting secrets in ordinary chat turns.
- Parsing `KEY=value` from arbitrary messages.
- Allowing model-selected arbitrary environment variable names.
- Replacing provider setup/OAuth flows.
- Claiming Matrix redaction erases homeserver backups, push notifications, bridges, or already-seen content.
- Disabling secret redaction.

## Threat Model

Protect against:

- malicious skill requesting dangerous or unrelated environment variable;
- secret appearing in gateway log preview;
- secret entering text batching, queued follow-up, clarify, slash command, agent transcript, compression, memory, or title generation;
- source event surviving in Matrix history;
- secret echoed in success/error response;
- retries persisting secret twice or to wrong target;
- newline/control-character injection into `.env`;
- unsupported managed environment writes being reported as success.

## User Experience

### Automatic skill setup

1. Agent loads a skill requiring missing `JACKETT_API_KEY`.
2. Gateway sends one secret-entry prompt:

   > Reply with the Jackett API key. Value will be consumed by gateway, not sent to model, and stored in this profile's `.env`. Matrix will attempt to redact the source message, but homeserver, bridges, notifications, backups, or clients may retain copies.

3. User sends the value as the next response.
4. Gateway consumes the value before normal dispatch, attempts source-event redaction, persists it, refreshes the active profile secret scope, and unblocks skill setup.
5. Gateway reports only:

   > Stored `JACKETT_API_KEY`. Value not shown.

No separate consent prompt, reaction, approval, configuration, or special reply gesture is required.

### Manual command

Optional follow-up:

```text
!secret set JACKETT_API_KEY
```

Command initiates the same single-prompt flow. It must never accept value inline:

```text
!secret set JACKETT_API_KEY actual-secret   # reject
```

Manual command is useful for rotation but not required for first implementation.

### Ordinary pasted secret

If no structured secret-entry prompt is pending, message remains ordinary chat input and existing redaction applies. Hermes does not heuristically persist arbitrary chat text.

## Security Policy

### Eligibility

Initial Matrix implementation requires:

- a pending secret-entry request created from structured local skill metadata;
- variable name declared by `setup.collect_secrets` or structured `required_environment_variables`;
- variable name passes existing `_ENV_VAR_NAME_RE` and denylist;
- no managed-scope override;
- adapter can intercept inbound text before normal dispatch;
- encrypted and unencrypted Matrix rooms follow the same flow.

No sender, room, thread, explicit reply target, profile, session, or expiry validation is required for accepting the value. These fields may be retained only as internal routing context needed to deliver the prompt and select the destination `.env`; they are not authorization checks and require no user configuration.

Secret collection uses one prompt. Sending the value is the action that authorizes storage. Terminal `/yolo`, reaction approval, and a separate consent prompt are not involved.

### Secret names

Model must not invent arbitrary destination variable names. Capture request receives normalized metadata generated by skill loader:

```python
SecretCaptureSpec(
    env_var="JACKETT_API_KEY",
    prompt="Jackett API key",
    skill_name="jackett-media-search",
    profile="entertainment",
)
```

Destination must originate from trusted local skill metadata after normal skill loading and validation. Consider optional global denylist for high-impact process controls (`LD_PRELOAD`, `PYTHONPATH`, `PATH`, `HERMES_HOME`, shell startup hooks, etc.). Existing `_reject_denylisted_env_var()` remains final enforcement.

### Source deletion and honest guarantees

Matrix redaction is best-effort deletion from current room history, not cryptographic erasure. UI text must say **redacted**, not “securely deleted.” Bridges, notifications, server logs, backups, and clients may retain copies.

Redaction is always best-effort cleanup, not a precondition for persistence or proof of erasure. The same rule applies to encrypted and unencrypted rooms. Server-side or client-side copies may survive in either case.

Recommended ordering:

1. receive and hold value in memory;
2. validate request and value;
3. attempt source-event redaction according to policy;
4. persist value if policy permits;
5. zero/drop references best-effort;
6. resolve waiter with metadata-only result.

Do not log redaction exception text if SDK could embed event content. Log class/code only.

## Architecture

### 1. Platform-neutral secret gateway primitive

Add `tools/secret_gateway.py`, modeled on `tools/clarify_gateway.py` but stricter.

Core entry:

```python
@dataclass
class SecretCaptureEntry:
    capture_id: str
    env_var: str
    skill_name: str | None
    destination_home: str
    delivery_context: dict
    event: threading.Event
    result: SecretCaptureResult | None = None
```

Public operations:

```python
register(spec, delivery_context) -> SecretCaptureEntry
get_pending() -> SecretCaptureEntry | None
resolve_with_value(capture_id, value) -> SecretCaptureResult
cancel(capture_id, reason)
wait_for_result(capture_id)
register_notify(callback)
unregister_notify(callback)
```

Registry must be lock-protected. Because no sender/room/thread/session binding is enforced, allow only one unresolved secret capture per gateway process. A second request fails as busy rather than ambiguously consuming a message for the wrong destination.

Result never contains value:

```python
@dataclass(frozen=True)
class SecretCaptureResult:
    success: bool
    stored_as: str
    validated: bool
    skipped: bool
    error_code: str | None
```

### 2. Replace global callback with context-safe routing

`tools/skills_tool.py` currently stores `_secret_capture_callback` as module global. This is acceptable for one interactive frontend, unsafe for concurrent gateway sessions/profiles.

Preferred change:

Use one process-wide callback and one process-wide pending capture. This matches the requested no-binding behavior while preventing two simultaneous requests from racing. The callback receives an immutable destination home resolved when the skill requests capture.

`_capture_required_environment_variables()` should:

1. build validated capture spec;
2. call callback for platforms declaring secure capture support;
3. keep current unsupported hint for other messaging platforms;
4. receive metadata-only result;
5. re-check persisted value through active profile store.

Remove broad `HERMES_INTERACTIVE` gate as capability decision. Replace with adapter/session capability query. `HERMES_INTERACTIVE` describes UI style, not whether platform can securely implement a workflow.

### 3. Base platform capability

Extend `BasePlatformAdapter` without adding model tool:

```python
supports_secret_capture: bool = false

async def send_secret_capture_prompt(
    self,
    *,
    entry: SecretCaptureEntry,
    prompt_text: str,
) -> SendResult:
    return SendResult(success=False, error="Not supported")

async def redact_inbound_secret(self, event: MessageEvent) -> bool:
    return False
```

Could use capability registry instead of a boolean if platform traits are centralized. Capability reflects whether Matrix adapter can intercept inbound text and attempt source-event redaction. Room type and encryption state do not alter eligibility or flow.

### 4. Gateway callback bridge

During gateway construction, register one secret notify callback next to existing interactive callback wiring.

Agent-thread callback:

1. validates metadata/spec and resolves destination profile home;
2. atomically registers the sole pending capture or returns `busy`;
3. schedules the single secret-entry prompt on event loop;
4. blocks until one value is received or gateway shuts down;
5. returns metadata-only result;
6. cleans up after success, persistence failure, cancellation, or gateway shutdown.

No synthetic user/assistant messages are appended. Secret-entry prompt is transport UI, not conversation content. Prompt cache and role alternation stay unchanged.

### 5. Pre-dispatch interception

Add interception near clarify response handling in `gateway/run.py`, before:

- inbound content logging;
- text batching;
- busy-input queueing and queued-follow-up merging;
- slash-command parsing;
- transcript/session persistence;
- model invocation;
- reply-context injection;
- title generation;
- compression/memory.

However current generic inbound log occurs late in `_handle_message`; Matrix adapter may batch text before gateway sees it. Adapter therefore checks the process-wide pending-secret slot before `_enqueue_text_event()`. Generic gateway owns persistence; adapter calls the common resolver.

Required order for Matrix message:

1. run existing platform authorization and event deduplication;
2. check whether one process-wide secret capture is pending;
3. if pending, consume the next non-command text message and do not enqueue or dispatch it;
4. attempt to redact source event;
5. persist value to the destination home captured when prompt was created;
6. send metadata-only acknowledgement;
7. return.

Commands such as `/stop` or `/cancel`, bot/system messages, media, edits, and empty values are not accepted as secret values. No sender, room, thread, reply-target, session, profile, or expiry match is performed.

### 6. Matrix implementation

Add `_MatrixSecretPrompt` state or reuse platform-neutral registry plus Matrix prompt-event map.

Matrix sends the secret-entry prompt directly. No reactions, buttons, consent prompt, or explicit reply relation are required.

Upon the next eligible text message while a capture is pending:

- bypass normal text batching;
- redact source event using internal adapter method regardless of model-facing `MATRIX_TOOLS_ALLOW_REDACTION` toggle; this is product-internal lifecycle cleanup, not agent tool permission;
- redact seed reactions and optionally prompt text after completion;
- never include value in logs;
- log only capture ID hash, env-var name, profile, outcome code, and source event ID hash if needed.

### 7. Persistence and live reload

Call `save_env_value_secure(env_var, value)` inside exact profile runtime scope.

Must fix current correctness gaps:

- `save_env_value()` prints and returns on managed denial, while `save_env_value_secure()` currently reports success unconditionally. Change save path to return/raise structured failure so messaging cannot claim stored when denied.
- After save, re-read profile `.env` and verify key exists without comparing/logging value.
- Refresh active secret scope so waiting skill can continue in same turn. In multiplex mode, rebuilding current context-local secret mapping is needed; mutating process `os.environ` is forbidden.
- In single-profile gateway, existing per-turn reload is insufficient for same blocked turn. Update active secret scope / controlled environment reader before skill readiness re-check.
- Do not restart gateway solely to use newly stored skill secret.

Provider credential lifecycle reconciles auth/config mirrors. Non-provider secrets such as `JACKETT_API_KEY` still safely land in `.env`; no provider mirror work occurs.

### 8. Logging and redaction

Current gateway log emits first 80 characters of inbound text before agent processing (`gateway/run.py`). Secret replies must never reach that statement.

Also audit:

- Matrix adapter body/debug logs;
- text batching logs (length only is acceptable);
- message persistence;
- delivery ledger;
- error logging and exception reprs;
- tracing/monitoring exporters;
- request dumps;
- tool-progress messages;
- Matrix reply fallback text;
- source event body retained in `raw_message`;
- session recap/latest-user-prompt;
- background notification payloads.

Add a first-class `MessageEvent.sensitive` or `consumed_control` marker only if needed. Prefer consume-and-return before event enters shared pipeline. If marker is added, every serializer/logger must fail closed.

Existing secret redactor is defense-in-depth, not primary protection. Random Jackett keys may not match known token shapes.

### 9. Skill metadata

Update affected skills to declare setup explicitly:

```yaml
setup:
  help: "Get key from Jackett settings"
  collect_secrets:
    - env_var: JACKETT_API_KEY
      prompt: "Jackett API key"
      provider_url: "https://jackett.sachi.m.mado.moe"
      secret: true
```

Skill prose should say:

- never place secret in ordinary chat;
- use structured Hermes secret prompt when offered;
- gateway consumes value before model sees it.

Remove absolute wording that implies Hermes can never receive a secret through any channel. Distinguish ordinary chat from structured capture.

## Fixed Behavior

Matrix secret capture requires no user configuration. Initial behavior is fixed:

- identical flow for encrypted and unencrypted rooms;
- no sender, room, thread, reply-target, profile, session, or expiry checks;
- one process-wide pending capture maximum;
- next eligible non-command text message supplies value;
- source redaction attempted best effort;
- 16 KiB maximum value size.

## Error Semantics

User-visible errors expose no value:

- `unsupported_platform`
- `redaction_unavailable`
- `redaction_failed`
- `cancelled`
- `invalid_env_name`
- `env_name_denied`
- `managed_credential`
- `value_empty`
- `value_too_large`
- `persistence_failed`
- `verification_failed`

Internal logs use same bounded codes. Never log raw exception if it can include secret or request body.

## Concurrency and Lifecycle

- Registry guarded by lock.
- Capture ID cryptographically random.
- Exactly one pending capture allowed per gateway process because no routing bindings are enforced.
- A second capture request returns `busy` and does not replace the first.
- `/cancel`, `/stop`, or gateway shutdown cancels pending capture without treating command text as secret.
- Duplicate Matrix delivery by event ID is idempotent.
- Resolver supports exactly-once persistence via `pending -> consuming -> stored|failed|cancelled`.
- Empty, oversized, media, edited, batched, bot/system, or command messages do not resolve capture.
- Python strings cannot be reliably zeroized. Avoid copying value and do not claim memory erasure; drop references best-effort after persistence.

## Test Plan

### Unit: `tools/secret_gateway.py`

- register/wait/resolve happy path;
- value absent from result object and repr;
- no sender/room/thread/reply/profile/session/expiry matching occurs;
- one global pending slot enforced;
- second request returns `busy`;
- duplicate resolve idempotent;
- cancellation unblocks waiter;
- no separate consent state or approval path.

### Unit: skill readiness

- supported messaging adapter invokes capture callback;
- unsupported adapter returns existing hint;
- declared env var accepted;
- undeclared/arbitrary env var rejected;
- managed/denylisted variable rejected;
- persisted key re-check changes readiness to available;
- destination home captured immutably when request is created.

### Unit: Matrix adapter

- next eligible text event resolves pending capture regardless of sender, room, thread, reply relation, profile, or session;
- encrypted and unencrypted rooms resolve through same path;
- command/media/edit/empty/bot events do not resolve capture;
- source message redacted before persistence callback when possible;
- redaction failure does not block persistence;
- secret message never enters text batch or `handle_message`;
- no body in caplog, including exceptions;
- duplicate event does not persist twice;
- bot lacking redaction power still persists and emits honest cleanup/retention status.

### Gateway integration

Use temp `HERMES_HOME` and real state DB/config loaders:

1. load fixture skill with `setup.collect_secrets`;
2. simulate Matrix secret flow;
3. assert `.env` mode and exact profile location;
4. assert no secret in state DB messages/system prompts/routing JSON/log capture;
5. assert waiting skill continues same turn;
6. assert second profile lacks key;
7. assert restart not required;
8. assert session history preserves role alternation and prompt hash.

### Security regression

Search all generated artifacts for sentinel secret:

- profile tree excluding intended `.env`;
- captured logs;
- state DB text columns/FTS;
- Matrix outbound messages;
- tool call/result payloads;
- delivery ledger;
- request dumps;
- memory and skill files.

Test random key shape that generic redactor does not recognize. This proves routing, not regex, protects value.

## Rollout

### Phase 1: generic primitive + Matrix capture

- no feature configuration required;
- structured skill setup only;
- one secret-entry prompt, no separate consent;
- next eligible process-wide inbound text supplies value;
- no sender/room/thread/reply/profile/session/expiry checks;
- encrypted and unencrypted rooms use equivalent flow;
- source redaction is always best-effort cleanup;
- no manual command.

### Phase 2: manual rotation command and more platforms

- `!secret set <declared-name>`;
- Telegram/Discord/Slack adapters where deletion semantics are adequate;
- platform-specific honest warnings.

### Phase 3: external secure form fallback

For platforms unable to delete/hide replies, issue short-lived localhost/dashboard URL or device-code-style flow. Secret enters HTTPS form, not messaging transcript. This is preferable to weakening guarantees for every platform.

## Alternatives Considered

### Accept any pasted secret after model says it is okay

Rejected. Value already entered logs/session/model path; model approval is not trustworthy authorization; destination may be attacker-controlled.

### Use dangerous-command approval or `/yolo`

Rejected. Command execution consent does not authorize credential disclosure, and `/yolo` must not weaken secret handling.

### Force `HERMES_INTERACTIVE=1` on Matrix gateway

Rejected. Flag does not provide UI transport or response resolver and can hang. Capability must be real and adapter-backed.

### Ask user to edit `.env` manually

Safe fallback, but poor automation and root cause of current refusal. Retain as fallback, not only path.

### Store secret from existing chat event after approval

Rejected for primary flow. Event may already be logged, persisted, batched, or sent to model before approval. Capture intent must exist before value arrives.

### External one-time web form only

Useful optional fallback, but more infrastructure than needed for Matrix capture. Consider for platforms without safe interception or deletion.

## Open Decisions

1. Store success prompt/event redaction policy.
2. Whether non-provider skill env vars should use a narrower writer than provider credential lifecycle.
3. Whether manual `!secret` command ships in phase 1.
4. Whether trusted bundled/local skill metadata is sufficient authorization for each env var.

## Recommendation

Implement Phase 1 with strict defaults:

- built in with no feature configuration;
- Matrix first;
- encrypted and unencrypted rooms use equivalent behavior;
- one secret-entry prompt, no separate consent or approval;
- next eligible process-wide inbound text supplies value;
- no sender, room, thread, reply-target, profile, session, or expiry checks;
- exactly one pending capture per gateway process;
- source redaction attempted immediately as best-effort cleanup;
- structured skill-declared env vars only;
- adapter-level interception before batching/logging;
- immutable destination home captured when request is created;
- verified persistence and metadata-only results.

This fixes over-conservative refusal without turning ordinary chat into secret storage. It extends existing setup architecture, keeps model outside secret boundary, adds no core tool, and preserves prompt caching.
