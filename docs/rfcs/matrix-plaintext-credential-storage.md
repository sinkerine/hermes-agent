# Matrix Plaintext Credential Storage

**Status:** Proposed replacement for custom-branch secure Matrix capture

**Scope:** local custom branch only

**Decision owner:** operator of trusted Matrix deployment

## Summary

Delete previous Matrix secure-prompt feature and replace it with explicit plaintext credential ingestion.

Matrix messages remain ordinary messages. Credential values may enter Matrix history, gateway logs, Hermes session history, model context, tool transcripts, memories, skills, tracing, backups, and other configured sinks. No interception, redaction, masking, special prompt state, or metadata-only result exists.

Add model-facing tool `store_matrix_credential`. Tool accepts destination environment-variable name and plaintext value, then stores value in active profile `.env`. Tool may also accept optional description/source metadata. Tool result confirms destination and persistence status; it must not echo value because echo adds no function, but no confidentiality guarantee is made.

This policy is intentional. Deployment treats these credentials as non-sensitive and wants full auditability in normal records.

## Goals

1. Remove all custom Matrix secure-prompt code, state, prompt wording, and tests.
2. Permit user to send credentials in ordinary Matrix text.
3. Permit model to persist supplied credential into active profile `.env`.
4. Preserve ordinary logging, session persistence, memory, skill-writing, tracing, and model visibility.
5. Keep profile routing correct under multiplex gateways.
6. Make insecure behavior explicit in tool name, description, docs, and tests.
7. Avoid automatic heuristic storage: persistence occurs only after explicit user intent plus tool call.

## Non-goals

- Confidential transport beyond Matrix deployment's existing properties.
- Secret redaction, masking, deletion, or secure erasure.
- Keeping values outside model context or logs.
- Pending captures, reply consumption, timeouts, redaction attempts, or consent overlays.
- Parsing every token-like string automatically.
- Writing process-wide `os.environ`; active profile `.env` is persistent destination.
- Weakening unrelated provider credential validation or non-Matrix secure prompts used by CLI, TUI, or Desktop.

## Rationale

Previous design optimized confidentiality:

- explicit secure prompt;
- adapter-level interception before dispatch;
- Matrix redaction;
- pending capture registry;
- metadata-only tool result;
- log/session/model exclusion.

Custom deployment wants opposite behavior. Credentials are intentionally non-sensitive. Normal Matrix and Hermes records are desired audit artifacts. Keeping secure-capture machinery creates complexity, hidden control flow, blocked turns, profile-routing state, and misleading guarantees.

Simpler contract:

1. user sends plaintext credential normally;
2. normal gateway pipeline records and dispatches it;
3. model calls persistence tool when requested;
4. writer stores value in active profile `.env`;
5. later tools resolve value through profile secret scope.

## User experience

### Direct storage request

User message:

```text
Store this as JACKETT_API_KEY in this profile: example-value
```

Message follows normal Matrix processing. Model calls:

```json
{
  "env_var": "JACKETT_API_KEY",
  "value": "example-value"
}
```

Hermes replies:

```text
Stored JACKETT_API_KEY in active profile environment.
```

### Skill setup

When skill reports missing environment variable, assistant may ask in ordinary Matrix chat:

```text
Send JACKETT_API_KEY here. It will be visible to Matrix, Hermes, model context, logs, and configured persistence.
```

User response is ordinary input. Assistant calls `store_matrix_credential`.

### Skill or memory retention

If user explicitly asks to include credential in skill text, memory, notes, or another artifact, normal write tools may do so. No credential-specific blocker or redactor is introduced by this feature.

## Architecture

### 1. Remove secure Matrix capture path

Delete custom-branch components:

- `tools/secret_gateway.py`;
- `tools/request_secret_capture.py`;
- `request_secret_capture` from `toolsets.py`;
- Matrix `_capture_home`, `_try_secret_capture`, shutdown cancellation, source-event redaction, and pre-authorization interception;
- gateway `TurnRunner` secure-capture callback, notifier, callback token, blocking waiter, and callback reset;
- Matrix credential-capture prompt guidance in `agent/prompt_builder.py` and SOUL/prompt policy text;
- secure-capture-specific skill readiness flow added by custom branch;
- tests dedicated to pending capture, redaction, metadata-only results, and request tool behavior.

Do not remove generic interactive `secret.request` support for CLI/TUI/Desktop unless separate scope explicitly requests it. This RFC removes previous **Matrix messaging** secure-prompt feature.

### 2. Add plaintext persistence tool

Add `tools/store_matrix_credential.py` in `messaging` toolset.

Schema:

```python
{
    "name": "store_matrix_credential",
    "description": (
        "Store plaintext credential received through ordinary Matrix chat in active "
        "Hermes profile .env. Value may already exist in Matrix history, logs, session "
        "history, model context, memories, skills, tracing, and backups."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "env_var": {
                "type": "string",
                "description": "Destination environment-variable name."
            },
            "value": {
                "type": "string",
                "description": "Plaintext value supplied by user."
            },
            "description": {
                "type": "string",
                "description": "Optional audit description."
            }
        },
        "required": ["env_var", "value"]
    }
}
```

Availability:

- enable for Matrix sessions through messaging toolset;
- reject non-Matrix invocation at runtime using session platform context;
- no feature flag and no secure-capture callback dependency.

### 3. Validation

Validate only storage correctness and process safety, not confidentiality.

Required checks:

- `env_var` matches `^[A-Za-z_][A-Za-z0-9_]*$`;
- preserve existing destination denylist for process-control names such as `PATH`, `PYTHONPATH`, `LD_PRELOAD`, `HERMES_HOME`, shell startup controls, and equivalent high-impact variables;
- reject NUL bytes and values unsupported by canonical `.env` writer;
- enforce bounded value size, default 64 KiB;
- reject managed credential destinations when canonical lifecycle says local write is unsupported;
- use exact value supplied; no trimming, normalization, repair, token parsing, or guessed key extraction.

These checks protect runtime integrity. They do not claim credential secrecy.

### 4. Persistence

Use existing profile-aware credential writer:

```python
save_env_value_secure(env_var, value)
```

Name is legacy; this design relies on writer correctness, atomicity, quoting, file mode, and provider reconciliation—not confidentiality semantics.

Execution must occur inside current turn's `_profile_runtime_scope`. Destination is `get_hermes_home()` resolved under that scope. Never infer profile from Matrix room name or process-global launch home.

After write:

1. verify canonical writer reports success;
2. reload active profile `.env` through `load_env()` or canonical secret loader;
3. assert destination key exists;
4. refresh current mutable secret scope so same turn can use stored value;
5. return structured status.

Do not mutate process-wide `os.environ` in multiplex mode.

Result shape:

```json
{
  "success": true,
  "stored_as": "JACKETT_API_KEY",
  "profile_home": "/resolved/profile/home",
  "validated": true
}
```

Tool should omit value from result because duplicate echo has no use. This is output minimization, not secrecy: input already exists in ordinary records.

### 5. Logging and records

No credential-specific suppression.

Allowed by policy:

- Matrix event history;
- gateway inbound previews and debug logs;
- Hermes session DB and FTS;
- model request/response traces;
- tool call arguments;
- skills, memories, notes, and generated files when requested;
- backups, bridges, notifications, homeserver logs, and observability exporters.

Do not add deliberate duplicate logging inside writer. Existing pipeline already records message and tool call. More copies add noise without audit value.

Existing global redactors may still transform recognizable values elsewhere. This RFC removes Matrix secure-capture guarantees; it does not require dismantling unrelated global redaction infrastructure used across providers and platforms.

### 6. Prompt policy

Replace secure-capture guidance with explicit plaintext policy for Matrix:

```text
Credential handling: Matrix credentials are ordinary chat data in this custom deployment.
When user explicitly asks to store a supplied credential, call store_matrix_credential with
exact destination name and value. Values may appear in Matrix history, Hermes logs, session
history, model context, skills, memories, traces, and backups. Never claim secure capture,
redaction, secrecy, or deletion.
```

Assistant may ask user for credential in ordinary Matrix chat only when destination and purpose are clear. Assistant must not invent destination names or silently persist unrelated text.

## Message flow

```text
Matrix event
  -> Matrix authorization / room policy
  -> normal MessageEvent construction
  -> text batching / queueing
  -> gateway logging
  -> session persistence
  -> model context
  -> store_matrix_credential(env_var, value)
  -> active-profile writer
  -> active secret-scope refresh
  -> normal assistant acknowledgement
```

No adapter-level early interception. No pending registry. No blocking gateway thread. No source redaction. No synthetic transport prompt.

## Concurrency and profile isolation

- Tool call belongs to one agent turn and one profile runtime scope.
- Concurrent turns in different profiles write different `.env` files.
- Existing credential lifecycle locking handles same-file write serialization.
- Same-key concurrent writes use writer's existing last-successful-write semantics; result reports only own write outcome.
- Two-profile test must prove A → B → A isolation and same-turn resolution.

## Migration plan

### Phase 1: remove old feature

1. Delete secure Matrix capture registry and request tool.
2. Remove callback wiring from gateway turn runner.
3. Remove Matrix interception/redaction path.
4. Remove secure-capture prompt guidance.
5. Remove obsolete tests and RFC statements.

### Phase 2: add plaintext tool

1. Implement `store_matrix_credential`.
2. Add to Matrix messaging toolset.
3. Wire active-profile persistence and live secret-scope refresh.
4. Add explicit insecure prompt guidance.

### Phase 3: clean historical policy

1. Update branch SOUL/prompt text that says Matrix credentials must use secure prompt.
2. Update relevant skills that prohibit ordinary Matrix credential entry.
3. Search docs for `request_secret_capture`, `secret_gateway`, `secure prompted capture`, and Matrix redaction claims.
4. Leave generic CLI/TUI/Desktop secure prompt docs unchanged unless they incorrectly claim Matrix behavior.

## Test plan

### Tool unit tests

- valid name and value persist successfully;
- invalid destination rejected before writer call;
- denylisted process-control destination rejected;
- value preserved exactly, including leading/trailing spaces and `=`;
- NUL and oversized values rejected;
- writer failure returns bounded error code;
- result omits plaintext value;
- non-Matrix session rejected.

### Matrix integration

Use temp `HERMES_HOME`, real state DB, real config loaders, and normal adapter dispatch:

1. send sentinel credential as ordinary Matrix text;
2. assert normal `MessageEvent` reaches gateway/model path;
3. invoke persistence tool with sentinel;
4. assert exact active-profile `.env` value;
5. assert same-turn secret lookup resolves value;
6. assert sentinel is present in normal session/log/tool-call artifacts configured by fixture;
7. assert no Matrix redaction call;
8. assert no pending-capture registry or blocked waiter exists.

### Multiplex profile integration

- profile A stores `A_KEY`;
- profile B stores `B_KEY`;
- A cannot read B and B cannot read A;
- return to A and confirm A still resolves `A_KEY`;
- default launch profile remains unchanged.

### Regression

- CLI/TUI/Desktop `secret.request` still works;
- provider credential lifecycle still reconciles expected mirrors;
- ordinary Matrix message authorization remains before dispatch;
- prompt cache remains stable because toolset and policy are fixed for session lifetime;
- role alternation unchanged.

## Acceptance criteria

- `git grep` finds no custom Matrix pending-capture registry or `request_secret_capture` implementation.
- Matrix plaintext credential messages follow normal dispatch and are not redacted by feature code.
- `store_matrix_credential` writes exact value to active profile `.env`.
- same turn can consume new environment value.
- multiplex profile isolation passes A → B → A integration test.
- ordinary records contain sentinel where fixtures enable them.
- user-facing text never claims secure capture, log exclusion, model exclusion, redaction, deletion, or secrecy.
- targeted test files pass through `scripts/run_tests.sh`.

## Risks

This design intentionally exposes credentials to every normal record sink. Compromise of Matrix account, homeserver, client, Hermes logs, session DB, traces, backups, skills, memories, or model provider may expose values. Retention and deletion become properties of each sink, not this feature.

Operator accepts this risk for custom deployment. Upstream/default Hermes should retain safer behavior unless maintainers explicitly adopt same policy.

## Recommendation

Implement as one behavior-changing series on `custom` branch:

1. removal commit for previous Matrix secure-prompt feature;
2. plaintext storage tool commit;
3. prompt/docs migration commit;
4. integration-test commit if test size warrants separation.

Keep change custom-branch-only. Name behavior honestly. Preserve profile correctness and writer validation, while removing confidentiality machinery and guarantees.
