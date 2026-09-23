# Credential-refusal removal: audit and implementation plan

**Status:** Audit and plan only; no runtime/prompt changes in this document.
**Scope:** Custom Hermes Matrix deployment, default profile plus all named profiles. Target symptom: assistant claims `KEEPASS_MASTER_PASSWORD` cannot be stored because no secure capture exists, then automatically declares chat credential exposed and requires rotation. Do not generalize this change to browser login/payment/2FA input, unrelated platforms, or provider-side model safety behavior.

## Evidence collected

- Custom branch `custom`, latest commit `c17eacb5ea`; working tree was clean at audit start. Existing Matrix plaintext storage design: `docs/rfcs/matrix-plaintext-credential-storage.md`.
- `agent/prompt_builder.py` contains Matrix-specific plaintext policy (`_MESSAGING_CREDENTIAL_CAPTURE_GUIDANCE`). `tools/store_matrix_credential.py` registers `store_matrix_credential` in `messaging` without a `check_fn` or `requires_env`. It writes through `save_env_value_secure`, verifies using `load_env`, and updates current secret scope.
- Eight named profile `SOUL.md` files still say `request_secret_capture` and prohibit ordinary chat solicitation and tool arguments. This directly contradicts current Matrix prompt/tool path. Default `~/.hermes/SOUL.md` had no matching guidance.
- `model_tools.py` adds a separate **browser-vault** instruction: never ask for or accept login/payment/2FA values in chat or type them through browser input tools. Browser-vault rules must not be removed as part of Matrix profile `.env` storage; distinguish them in prompt wording.
- Active KeePassXC skill and eight named-profile copies use `KEEPASSXC_PASSWORD`, not `KEEPASS_MASTER_PASSWORD`. Default and all eight named profile `.env` files have `KEEPASSXC_PASSWORD` present, `KEEPASS_MASTER_PASSWORD` absent, and mode `0600`. Name mismatch needs explicit decision; never silently alias, copy, or overwrite either key.
- `secret-backed-profile-rollouts` and its diagnostics reference already permit explicit Matrix storage; its broader default secret-handling language still says not to place values in logs, command arguments, skills, or replies. `keepassxc-unlock-search` explicitly forbids GUI typing of master password and prefers env-to-stdin CLI use; distinguish storage from GUI typing.
- A source scan did not locate the quoted refusal text in custom source or active profile prompt files. Exact failing assistant turn and toolset were not recovered from current session database search. Therefore root cause remains a combination of **confirmed stale profile policy** and **unconfirmed model/session context**; do not claim a particular layer generated the exact words without a reproduced trace.

## Contract before editing

1. Explicit Matrix request to store a user-supplied value under an exact, valid destination name invokes `store_matrix_credential` in the active profile; verify write via readback without printing value. When value or destination is missing, ask only for missing input; never invent a value or variable name.
2. Do not automatically claim chat delivery makes a credential compromised or prescribe rotation as a precondition. State actual record/retention exposure honestly when relevant; never claim secret capture, deletion, or redaction. If user requests risk assessment, explain tradeoffs without blocking storage.
3. Credential storage is not browser form filling. Preserve browser-vault-only login, checkout, and 2FA handling; preserve authorization and process-control env-name denylist, NUL/size checks, atomic writer and private file permissions, profile isolation, explicit intent gate, and ordinary non-Matrix secure prompt behavior.
4. For KeePassXC, resolve requested key exactly: deployment currently uses `KEEPASSXC_PASSWORD`; `KEEPASS_MASTER_PASSWORD` is distinct. Confirm destination before migration/aliasing. Existing skill's GUI prohibition and CLI stdin rule remain until separately scoped.

## Change plan

1. **Reproduce and trace:** locate failing turn from correct named profile/session, inspect only metadata/tool names and sanitized user/assistant turns; determine profile, platform, toolset, whether `store_matrix_credential` was offered, and whether `SOUL.md`, memory, active skill, prior refusals, or browser-vault guidance entered model context. Do not dump secret values.
2. **Profile prompt cleanup:** replace obsolete secure-prompt paragraph in each of eight named `SOUL.md` files **in place** with narrow Matrix plaintext storage instruction; fix entertainment profile's extra obsolete secure-gateway paragraph. Leave unrelated SOUL content unchanged. Default SOUL can retain no override because source hint already defines policy, or add equivalent instruction if test shows need.
3. **Skill alignment:** reconcile active `secret-backed-profile-rollouts` and all profile-specific KeePassXC skill copies with this contract. Preserve no-value-printing and consumer verification as operational defaults, but eliminate any blanket claim that a chat-provided Matrix credential cannot be accepted/stored. Keep GUI/browser password entry distinctions. Inspect active memories and policy task files in affected profile; edit only contradictory instructions, not historical transcripts or backups.
4. **Source/tool audit:** inspect prompt assembly and per-platform toolset resolution for the failing profile, including disabled `messaging` toolset; if unavailable, fix scoped registration/toolset configuration. Confirm writer destination, denylist, exact-byte preservation, same-turn reload, and multiplex isolation. Avoid removing browser vault rewriting or generic `secret.request` paths.
5. **Regression tests:** add behavior contracts for Matrix prompt/registry/tool availability, exact storage in temp `HERMES_HOME`, denial of invalid names, A→B→A profile isolation, missing destination requiring clarification, and unaffected browser-vault/CLI-TUI paths. Run `scripts/run_tests.sh` on affected files; use harmless sentinels, never real credentials.
6. **Deploy:** commit source changes on active custom branch after tests; update profile SOUL/skill layers separately (they are outside repo). Restart affected gateways only for runtime changes; verify all services active, intended repo/venv command lines, Matrix connection, and new-session prompt assembly. Existing sessions retain cached prompts and refusal transcript: use a fresh Matrix thread/session or `!reset` where supported; do not assume restart repairs old context.
7. **Acceptance probe:** in fresh Matrix session, request storage of a harmless synthetic value with explicit destination; confirm tool call, exact active-profile `.env` readback, absence from other profiles, no false secure-capture or mandatory-rotation claim. Do not test by restating real KeePassXC password.

## Open decisions / boundaries

- Which named profile and Matrix session produced exact refusal? Needed for definitive attribution.
- Did user intend `KEEPASS_MASTER_PASSWORD` as new key, or existing `KEEPASSXC_PASSWORD`? Never infer equivalence.
- This plan removes **incorrect Matrix refusal and mandatory rotation guidance**, not every credential security control in Hermes or browser vault.
