# Matrix interactive UI disposable E2E sandbox

This is T8 of [the approved plan](../../../docs/features/matrix-ui-plan.md), especially §4,
§5 and D-2. A real Synapse, the current-worktree Hermes gateway and a fresh pinned Element
build are required to establish E2E behavior. Offline checks and model protocol evaluation
cannot replace encrypted-room browser evidence. No E2E completion is claimed by preparing
this harness.

All mutable assets, credentials, logs, caches, crypto stores and screenshots belong under
`$PWD/.hermes-sandbox/matrix-ui` (git ignored). Never use production config, accounts,
rooms or personal devices. Do not source `activate`, repurpose `HOME`, provision a new
Python environment, install host libraries, or edit `/home/sinkerine/dev/element-web`.
The supplied test interpreter is used exactly; if it is missing, stop and report the blocker.

## Prepare and inspect without starting services

Run each command from the worktree root. Every Python/runner/PM command must follow
`set -e`, source this environment and retain its exact home/runtime/interpreter assertions.
The environment also redirects Python bytecode, Ruff, Node/Corepack/pnpm and temporary caches.
It never changes `HOME`. Shell scripts source it themselves.

```bash
set -e
source scripts/sandbox/matrix_interactive_ui/env.sh
bash -n scripts/sandbox/matrix_interactive_ui/*.sh
python -I -X pycache_prefix="$SB/cache/pycache" -m py_compile scripts/sandbox/matrix_interactive_ui/*.py
python scripts/sandbox/matrix_interactive_ui/validate.py
python scripts/sandbox/matrix_interactive_ui/lifecycle.py preflight
python -m ruff check scripts/sandbox/matrix_interactive_ui
python scripts/check-windows-footguns.py scripts/sandbox/matrix_interactive_ui
python -m scripts.code_health --base HEAD
ss -ltn '( sport = :18008 or sport = :18081 or sport = :18082 )'
docker version --format '{{.Server.Version}}'
docker image inspect matrixdotorg/synapse@sha256:1909bcc07d4180945464712a7f4d4bb6f644c4c1774d026d77cdadc196b91eb8 --format '{{.Id}}'
git -C /home/sinkerine/dev/element-web cat-file -e bcb080171cb312feef37e63007c7ca8cc0a8b77f^{commit}
```

`validate.py` runs both direct `present_ui` and deferred
`tool_search({queries:["present_ui"]})` → `tool_describe({names:["present_ui"]})` →
`tool_call({calls:[{name:"present_ui",arguments:{ui:...}}]})` sequences. Each card gets an
invalid root, the strict parser's `interactive_ui: root: unknown_ref` error, one correction,
and a brief final response. Real catalog search, describe, bridge resolution and registry
dispatch run against a synthetic protocol-only handler; these checks make no delivery claim.
It also exercises harmless environment sentinels, sandbox cache propagation,
leader-exit/surviving-descendant cleanup, PID-start-time refusal, command timeout, SIGINT/SIGTERM,
synthetic store identity/revision checks, Element Labs config regeneration, evaluator transport failures,
persistent budget migration/exhaustion across simulated restarts, explicit limit forwarding, targeted
case selection and distinct evaluation receipts. It starts only bounded
disposable local Python processes and creates synthetic stores under sandbox tmp; it never
starts services or reads credentials. Results are saved to `evidence/offline-review.json`.

Preflight reports availability and writes `evidence/preflight.json`; it does not start a
service, install packages, read provider credentials or reach Magpie. All three ports must
be free before `up`. Existing unrelated port 8080 is left alone. Docker's published Synapse
port is `127.0.0.1:18008`, Element is `127.0.0.1:18081`, provider is `127.0.0.1:18082`.
The Synapse image is pinned to the locally available digest, rather than `latest`.

## Start after harness review

`pnpm` must already be available. Building can require network access and take several
minutes. The harness archives Element commit `bcb080171cb312feef37e63007c7ca8cc0a8b77f`
from the reference checkout into `element-src`, then runs these commands there through
`lifecycle.run` (sanitized environment, owned process groups, 1800-second deadline each):

```bash
pnpm install --frozen-lockfile --ignore-scripts --store-dir "$SB/cache/pnpm-store"
pnpm --filter element-web rebuild @element-hq/element-call-component matrix-js-sdk
pnpm --config.verifyDepsBeforeRun=false --filter element-web run build
```

The root postinstall invokes Desktop's `electron-builder install-app-deps`, which requires
unneeded native compiler setup. Skipping install scripts avoids that hook; the explicit
Web dependency rebuild restores the prepared Git packages (especially Element Call's
`dist/`). Nx then builds Web and its workspace dependencies. Disabling pnpm's automatic
dependency verification prevents it from reinstalling with the Desktop hook enabled.
Do not omit the rebuild: an ignored-scripts install alone leaves Element Call unbuilt.
It does not reuse the reference checkout's old `webapp` or dependencies. The lockfile and
transitive dependencies belong to that pinned external project; Hermes dependency files
are unchanged. Build output and caches stay inside the sandbox. A failed/incomplete build
requires explicit purge before another `--build-element` run.

```bash
set -e
source scripts/sandbox/matrix_interactive_ui/env.sh
scripts/sandbox/matrix_interactive_ui/up.sh --build-element
```

For a subsequent run retaining a completed build:

```bash
set -e
source scripts/sandbox/matrix_interactive_ui/env.sh
scripts/sandbox/matrix_interactive_ui/up.sh
```

Registration is closed. `bootstrap.py` creates random passwords for `@hermes-bot:localhost`,
`@alice:localhost` and `@bob:localhost`; `accounts.json`, bot token, recovery key and all
other harness-written files use mode 0600 under a private umask. Bot login has a stable
persisted device; restarting never resets its crypto database. A partial registration fails
closed: purge the disposable run before retrying. Existing unowned home config is refused.
The gateway uses the real `python -m gateway.run` entry point, raw `matrix` settings,
`platform_toolsets.matrix: [interactive_ui]`, explicit allowed users and required E2EE.
The selected platform toolset deliberately restricts this experiment to `present_ui`.
Bootstrap, install/build commands and services share the same allowlisted child environment.
It retains sandbox Node/Corepack/pnpm/cache paths and removes inherited credentials and execution
controls such as `NODE_OPTIONS`, `PYTHONPATH`, `BASH_ENV` and npm script-shell overrides.
The fake provider follows the tools in each actual OpenAI request: directly listed `present_ui`
is called directly; a deferred surface is searched and described before invoking the `tool_call`
envelope. Catalog results are paired to call IDs and do not consume card correction attempts.
After the deliberate invalid-root error it sends the corrected fixture through the same surface;
the real producer validates, uploads media, encrypts, sends and stores each card. No toolset is
changed mid-turn. Deterministic provider receipts retain only tool names and parameter schema
structure (type, required, properties, items), excluding prompts, values, descriptions and headers.

Do not print `accounts.json`, `.env`, recovery keys, nonces or decrypted action values into
shared logs. For browser automation read only the selected disposable account fields in
memory and pass them directly to password/user input actions. Do not screenshot login forms
or developer tools containing credentials. CLI/browser connectors are discovered by the
parent at E2E time; no particular connector is assumed installed by this harness.

Setup repair on this host used the sandbox-cached pnpm 12.11.2 directly. The existing
`$SB/bin/pnpm` symlink points to
`$SB/cache/corepack/v1/pnpm/12.11.2/bin/pnpm.mjs` (executable), bypassing Corepack's nested
package-manager mismatch. Before invoking the harness here, after sourcing `env.sh`, use
`export PATH="$SB/bin:$(dirname "$HERMES_PYTHON"):$PATH"`. No upstream source or lockfile
changes are required. `evidence/element-build-recipe-result.json` records the validated
commands; `evidence/element-build-provenance.json` records source and output hashes.

The browser setup smoke used Playwright 1.64.0 already present at
`$SB/element-src/node_modules/.pnpm/playwright@1.64.0/node_modules/playwright` with existing
Nix-packaged Chrome:
`/nix/store/dqzn64123p2qvidnx0wspr0z96rc407a-google-chrome-156.0.8078.79/bin/google-chrome-stable`.
Launch from `cwd=$SB/tmp`, pass browser-local `TMPDIR=.` / `TMP=.` / `TEMP=.`, and put
`userDataDir`, `XDG_CONFIG_HOME` and `XDG_DATA_HOME` under `$SB`. The relative temporary
path avoids Chromium's 108-byte Unix socket path limit while keeping files inside the
sandbox. Leave `HOME` unchanged and set `chromiumSandbox: true`. Run Node through
`lifecycle.run` with an allowlisted environment; do not inherit provider credentials.
`$SB/evidence/chromium-smoke.cjs` retains the exact successful launch and screenshot recipe;
`evidence/chromium-smoke.json`, `evidence/chromium-smoke.log` and
`evidence/screenshots/00-chromium-smoke.png` establish a 1280×900 static-page launch only.
The downloaded Chromium 1208 had all libraries resolved from existing Nix paths in
`evidence/chromium-libraries.json` but still exited with SIGTRAP; the packaged Chrome
wrapper supplies a working library closure without host installation. This setup smoke
does not establish Element rendering or any Matrix journey step.

## Browser journey and evidence

Element config uses `features.feature_interactive_ui: "labs"` with `show_labs_settings: true`.
At the pinned commit, `ConfigSettingsHandler.getValue` maps `"labs"` to no config override;
`SettingsStore.settingIsOveriddenAtConfigLevel` locks device settings only for boolean config
values. A config value of `true` forces the Labs switch on and disables it. Regenerate an
existing build's config without starting or stopping any service:

```bash
set -e
source scripts/sandbox/matrix_interactive_ui/env.sh
python scripts/sandbox/matrix_interactive_ui/lifecycle.py configure-element
```

Reload Element to read the new config. In Settings → Labs, enable Interactive UI for Alice
and disable it for Bob using each account's normal switch. These are separate device settings;
keep both browser profiles under the sandbox and record the switch states in browser evidence.
The offline config check validates generated configuration only; browser checks establish that
the pinned Element exposes working switches.

Navigate a browser capable of reaching this host's loopback to `http://127.0.0.1:18081`.
Use separate browser profiles/contexts for Alice and Bob, retaining each sandbox context
across screenshots. Paseo navigate/type/click/resize/screenshot tools or Playwright from an
already installed runtime can drive the same steps. Browser storage/downloads must be under
`$SB`; do not launch against a personal browser profile. A remote browser whose loopback is
another machine is a blocker until a loopback-preserving connection is available.

1. Log Alice in with the private local fields, choose the custom server
   `http://127.0.0.1:18008` if prompted, create an **encrypted direct chat** with
   `@hermes-bot:localhost`, and wait for the bot to join. Confirm room encryption from
   room settings and the encrypted event type. Save `evidence/screenshots/01-encrypted-dm.png`.
   Verify/trust only these disposable devices as needed. Save browser storage only inside `$SB`.
2. Send `[case:graph-zh] 用知识图谱解释《风味的法则》里成分—机制—结果的关系，并允许按知识分支切换`.
   The deterministic provider searches/describes a deferred tool when required, calls
   `present_ui` through the request's direct or bridge surface with an invalid root, corrects
   it once after the real path-specific tool error, then replies briefly. Check one actual card, no visible
   fallback, heading, branch selector and layered graph. Capture
   `02-graph-zh-1280.png` at viewport 1280×900 and `02-graph-zh-360.png` at 360×800.
3. Change branch and selected mechanism locally. Check no new Matrix event, provider request,
   or gateway turn. Click the repeatable explanation button. Check exactly one action and one
   continuation, selected values reflected in the answer, and no submitted edit. Capture
   `03-selected-mechanism.png`. The deterministic receipt records `action-continuation`.
4. Send `[case:graph-en] Explain a cooking knowledge map`, `[case:table] Compare cookers`,
   `[case:chart] Study hours chart`, then `[case:form] Plan my week` as separate messages.
   Check English graph, sortable table, chart data view, and form. Capture
   `04-graph-en.png`, `05-table-sorted.png`, `06-chart-data.png`, `07-form-active.png`.
5. Before submitting the form, obtain its **original** event ID through Element's event
   information (decrypted content view), start the observer below in another shell, then
   submit immediately. Capture `08-form-submitted.png` and a browser trace/video or Matrix
   event timestamps proving the visible submitted edit within eight seconds of click and
   before the model reply. The observer independently times the durable store commit only.
   Check exactly one continuation. Successful confirmation disables the client's **Try again**
   path; it cannot be used on this submitted card. Use the store-verified inactive replay probe
   in step 7 to check one inactive notice with no new turn (`09-form-inactive.png`).
6. Open a thread from an Alice root message, ask for another graph and form inside it, and
   repeat the action checks. Card, action, status edit and continuation must remain in the
   same `m.thread` relation. Capture `10-thread.png`; preserve original/action/edit IDs and
   relation metadata in a private evidence receipt.
7. Export one original decrypted **action content object** (not the event envelope) from
   Element's event information to `evidence/action-content.json`, mode 0600. Preserve the
   exact nonce and `m.relates_to` thread relation. Run replay as below. The replay helper logs
   in as Alice on its own persistent E2EE device, joins no rooms automatically, requires
   bot membership, and uses the actual mautrix crypto store. Compare gateway turn counts
   and provider receipts before/after. Duplicate should be silent with no new turn. Repeat
   after gateway restart. Stale must use a fresh nonce and an older revision; inactive uses
   a fresh nonce and the latest consumed revision. Neither may dispatch a turn.
8. Log Bob in using a separate context. Turn **off** Settings → Labs → interactive UI
   (`feature_interactive_ui`) for Bob, reload and confirm that the same cards render complete
   fallback. Invite Bob to the disposable room and ensure history/key access; if historical
   keys cannot be shared, send fresh cards after Bob joins. Save `11-fallback-1280.png` and
   `11-fallback-360.png`, checking graph nodes/edges, table rows, chart values, form options
   and actions. Do not claim absence of fallback when the whole message is undecryptable.
9. Alice sends `[case:carousel] Show three destinations`. Fixture images are valid local PNGs
   generated under `assets`; the real producer must upload encrypted attachments. Confirm
   all three decrypt, navigation and “Item n of m”; save `12-carousel.png`. These tiny test
   images prove transport only, not visual image quality. Also send `[case:fact] Capital of
   France?` and `[case:joke] Tell a programming joke`; expect text and no card.

Create `$SB/evidence/screenshots` before saving screenshots. Record UTC timestamps, browser
version, viewport, source HEAD, Element commit, provider mode, event IDs, revisions and byte
sizes in `evidence/journey.md`; never copy secrets or action values into that report.
Keep raw gateway logs private and redact before sharing; production logging behavior is not
weakened by the harness. Screenshots, event timing/relations, gateway logs and independent
provider request counts together establish the journey. Record each step as passed, failed
or blocked, with the actual evidence path. Missing screenshots are not a pass.

```bash
set -e
source scripts/sandbox/matrix_interactive_ui/env.sh
mkdir -p "$SB/evidence/screenshots"
read -r -p 'Original disposable UI event ID: ' MUI_EVENT
python scripts/sandbox/matrix_interactive_ui/observe_status.py --event "$MUI_EVENT"
```

Replay commands prompt for the disposable room ID without assuming a fixed room identifier:

```bash
set -e
source scripts/sandbox/matrix_interactive_ui/env.sh
read -r -p 'Disposable room ID: ' MUI_ROOM
python scripts/sandbox/matrix_interactive_ui/replay_action.py --room "$MUI_ROOM" --mode duplicate
python scripts/sandbox/matrix_interactive_ui/lifecycle.py restart-gateway
python scripts/sandbox/matrix_interactive_ui/replay_action.py --room "$MUI_ROOM" --mode duplicate
```

Wait for gateway reconnection in `evidence/gateway.log` before the second replay. Retain the
original consumed form action export (normally revision 0). Once its submitted edit is visible,
run the probes against the same room and export:

```bash
set -e
source scripts/sandbox/matrix_interactive_ui/env.sh
python scripts/sandbox/matrix_interactive_ui/replay_action.py --room "$MUI_ROOM" --mode stale
python scripts/sandbox/matrix_interactive_ui/replay_action.py --room "$MUI_ROOM" --mode inactive
```

The helper reads only the sandbox UI store, verifies room ID, original event ID and UI ID,
and requires a stored inactive card at revision >0. Stale gets a fresh nonce and latest
revision minus one; inactive gets another fresh nonce and the current stored revision.
Duplicate preserves the original content and nonce exactly. Neither probe needs an action
export from a submitted card. Verify distinct stale/inactive notices and no new turns.

The overdue **Try again** client path is a separate optional scenario on a new active form.
It requires controlled interruption of confirmation delivery to that test browser after the
action reaches the gateway, while keeping the browser's payload active until retry appears.
Record the exact interruption, restoration, browser and server event timings, and retry
outcome. This harness does not automate selective delivery interruption; mark this scenario
blocked unless a reviewed browser transport control can establish those conditions. A normal
successful submission or store-only replay cannot establish the overdue client path.

If E2EE key/device verification blocks this helper, create an
**unencrypted disposable sibling room**, have Alice and the bot join, generate/act on a new
card there, export that room's action, and use `--allow-unencrypted-sibling`. Never silently
retry an encrypted-room action as plaintext or transplant its room/event IDs. The receipt
records the limitation; encrypted replay remains unverified.

## Quality evaluation and live mode

Deterministic protocol evaluation uses the real `PRESENT_UI_SCHEMA` and strict parser with
nine prompts from `eval_prompts.yaml`, at most three requests each. It records tool use,
retry count, validation errors, self-correction and UI usefulness. Evaluator HTTP requests allow
105 seconds, exceeding the proxy's 90-second upstream deadline. Transport failure produces a
structured failed-case receipt with attempted request count and a budget-may-be-spent marker;
the persistent live budget ledger remains authoritative and is never reset by evaluation.
Evaluation does not call the producer: successful results explicitly say no delivery occurred, and local image upload
validation is marked unverified. This quality receipt cannot stand in for Matrix/browser E2E.

```bash
set -e
source scripts/sandbox/matrix_interactive_ui/env.sh
python scripts/sandbox/matrix_interactive_ui/evaluate.py --mode deterministic
```

To run actual bounded Magpie evaluation after review, first stop services, then explicitly
start live mode. Only this mode selectively reads the matching provider's `baseUrl` and
`apiKey` from `~/.pi/agent/models.json`; the key stays in provider process memory and is never
printed or copied to `.env`. A missing/ambiguous entry or unexpected endpoint fails closed.
The gateway still sends only to the loopback proxy, which pins `gpt-6.1-sol`, strips inherited
credentials, disables streaming and defaults to at most **30 requests total**, across restarts,
including browser turns, retries, auxiliary calls and the prompt evaluation. An operator may pass
`--live-request-limit N` with an explicitly authorized total cap from 1 through 42. The ledger's
`limit` retains the highest explicitly configured authorization; `reserved_requests` is never reset.
Omitting the flag preserves an existing ledger limit and also enforces a runtime cap of 30, so an
extension requires the explicit flag again after restart. Reservations are persisted before
forwarding; transport failures spend budget, retries cannot bypass it.
The budget ledger in evidence survives purge. To give the nine-prompt evaluation its full
27-request allowance, run it before live browser prompts. Do not erase the ledger to evade
the bound. If budget remains, spend it on the browser journey and report the resulting
coverage; a full deterministic journey and bounded live sample have separate receipts.

```bash
set -e
source scripts/sandbox/matrix_interactive_ui/env.sh
scripts/sandbox/matrix_interactive_ui/down.sh
scripts/sandbox/matrix_interactive_ui/up.sh --live
python scripts/sandbox/matrix_interactive_ui/evaluate.py --mode live
```

The first live evaluation exhausted all 30 reserved slots (nine TLS failures and 21 actual
requests). Its forced-invalid case failed after `root: unknown_ref`, then `nodes[0].gap: bad_value`
with an empty hint. The schema permits gap values `none`, `sm`, `md`, `lg`; schema-derived
validation hints now describe enum and type constraints, and the tool catalog lists gap values.
The user subsequently authorized **up to 12 additional requests, total persistent cap 42**.
This authorization has not been spent by the offline implementation checks; the historical
[verification report](../../../docs/features/matrix-ui-verification.md) remains a record of the
failed first evaluation until actual new results are reviewed. No quality pass is implied.

After parent review, the authorized targeted retry is:

```bash
set -e
source scripts/sandbox/matrix_interactive_ui/env.sh
scripts/sandbox/matrix_interactive_ui/up.sh --live --live-request-limit 42
python scripts/sandbox/matrix_interactive_ui/evaluate.py --mode live --case invalid
scripts/sandbox/matrix_interactive_ui/down.sh
```

`--case` may be repeated to select other named cases; invalid ids fail before requests.
Each selected case still permits at most three requests. Every evaluation writes a new
`evidence/<mode>-eval[-<case>...]-<unique-id>.json` receipt and prints its path, including on
a failed forced-invalid gate. Runs without the invalid case record its correction criterion
as null (not evaluated). Original `live-eval.json` and earlier failed receipts are retained.
The 42-slot ledger covers all traffic while live mode runs; reserve remaining slots for
review-directed retries only, and inspect the ledger between attempts. Starting live mode
also starts the gateway, so avoid unrelated prompts during a targeted evaluation.

Receipts include historical `evidence/deterministic-eval.json`, `evidence/live-eval.json`,
new unique evaluation receipts,
`evidence/deterministic-provider.jsonl`, `evidence/live-provider.jsonl`,
`evidence/live-budget.json`, `evidence/replay-*.json`, `evidence/status-*.json`,
`evidence/gateway.log`, `evidence/element-build.log`, `evidence/synapse.log`,
`evidence/preflight.json`, `evidence/journey.md`, and `evidence/screenshots/`.
The forced-invalid case must self-correct; other quality judgments are descriptive, not a
blanket pass/fail gate. Human review assesses readable labels, useful content, appropriate
interaction and unnecessary UI. Transport/schema success alone does not establish quality.
If Magpie is unreachable, leave live results unverified and keep deterministic evidence.

## Stop and purge

```bash
set -e
source scripts/sandbox/matrix_interactive_ui/env.sh
scripts/sandbox/matrix_interactive_ui/down.sh
```

Commands (including bootstrap and long builds) and services have owned session/group receipts.
A guardian preserves the group leader's identity until live descendants exit, including when
the command leader exits first. Stop checks `/proc` start time, session and owner receipt,
then signals the owned group, escalates after a bounded grace period and verifies no live
members remain before removing the receipt. Zombies cannot write and are excluded.
Unverifiable identity or surviving members retain the receipt and block purge. Container removal requires the exact worktree label and unique name.
Names/PID text alone never authorize stopping unrelated processes. Timeout, SIGINT, SIGTERM
and failed startup perform owned cleanup; evidence remains. Detached descendants that leave
the owned session/group are outside this process-group contract; build scripts must not detach. This harness intentionally requires Linux `/proc`.

```bash
set -e
source scripts/sandbox/matrix_interactive_ui/env.sh
scripts/sandbox/matrix_interactive_ui/down.sh --purge
```

Purge is explicit and guarded by the worktree owner marker and resolved paths. It removes
only harness-owned Synapse/build/accounts/replay/media/run state, gateway config, Matrix
crypto/UI store, and session files. It keeps evidence, caches and the existing PM-provisioned
interpreter shared with earlier feature tasks. Evidence contains private sandbox content;
review it before sharing. No generated asset or secret belongs in a commit.

After implementation changes, run the approved §4 feature/regression/full checks through
`scripts/run_tests.sh` and `python scripts/check` in the asserted sandbox environment. Record
unrelated failures rather than claiming a full pass. A blocked Element dependency install,
missing browser connector/native library, unreachable Magpie, or unverified crypto device
is reported as a blocker, not replaced by mocks or fabricated screenshots.
