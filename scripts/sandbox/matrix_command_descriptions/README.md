# Matrix command descriptions browser sandbox

This harness proves MSC4391 publication with a real Synapse, the current worktree's
`python -m gateway.run`, and Element at `d698eb223e7dc3030fed2c49330e9ecb98b4ab0d`.
Element's original checkout is read-only. The harness archives that commit into its
own sandbox, builds it there, and checks commit, lockfile and webapp digests before
and after browser/conformance runs. It never substitutes synthetic publication for
browser evidence. Missing Chrome, dependencies, network or a failed build is a blocker.

E2E runs **when the feature is first added only**. Unit tests always run on future
pushes. Re-run this journey when its integration changes. Desktop E2E is excluded.
T2 implements this recipe offline; building Element and starting services belong to
the later approved feature journey.

## Boundaries and setup

Run in the feature worktree root on Linux. First provision the isolated PM `.venv`
with Matrix encryption extras through the project's supported PM workflow. This
harness neither provisions Python nor changes HOME. Source:

```bash
source scripts/sandbox/matrix_command_descriptions/env.sh
python scripts/sandbox/matrix_command_descriptions/harness.py selftest
```

`SB=$PWD/.hermes-sandbox/matrix-commands`; Hermes home, runtime, caches, browser
profiles, TMP, Element dependencies, process receipts, secrets and evidence all
live beneath it. `$SB/bin` precedes `.venv/bin` on PATH. Secrets are private 0600
files. Only generated local accounts are used: bot `@hermes:localhost`, allowed
Alice and denied Bob. The bot has a stable device and requires E2EE. No production
profile, remote browser or live model is used. The loopback stub counts all model
requests; the entire command journey requires zero completions.

Dependencies: Docker with the pinned Synapse image already local; git, tar, Node,
Corepack, build prerequisites/network for Element; and Nix Chrome at
`/nix/store/dqzn64123p2qvidnx0wspr0z96rc407a-google-chrome-156.0.8078.79/bin/google-chrome-stable`.
Playwright comes from the archived Element dependency installation. Chrome uses
its sandbox, persistent Alice profile, sandbox TMP cwd and a relative TMPDIR.
Synapse/Element/provider bind only 127.0.0.1 on ports 18108/18181/18182.

Containers are named `hermes-mcd-<owner16>` and labeled
`org.hermes.matrix-command-sandbox=<owner16>`. Receipts identify process group
leader PID, Linux starttime and owner. Teardown refuses foreign/reused identities.
Runtime and Synapse ownership markers prevent adoption of existing data. Logs and
dumps may contain local room content; retain them privately.

## First-add browser journey

Use the shorthand below after sourcing env.sh:

```bash
h() { python scripts/sandbox/matrix_command_descriptions/harness.py "$@"; }
h preflight
h build-element
# After an interrupted build only: h build-element --resume
h verify-element
h up
h browser --step login-and-dm
```

`build-element` installs pnpm 12.11.2 through sandbox Corepack and runs the pinned
frozen install with scripts ignored. Git-hosted Element Call has only a `prepare`
script; pnpm rebuild cannot recover the source omitted from its installed package.
The harness downloads the exact Call revision in Element's lockfile, verifies the
archive's SHA512 integrity against that lock, installs Call with its own unchanged
frozen lock and runs its declared `build:component`. Those two Call commands alone
set `MCD_CALL_BUILD=1`, which makes the sandbox pnpm shim pass
`--pm-on-fail=ignore` to each nested pnpm invocation: Call's nested SDK requests
pnpm 12.3.4, while this harness deliberately retains pnpm 12.11.2. An env-only
`npm_config_pm_on_fail` override did not reach the nested check. This approved CLI
compatibility override does not alter source metadata, package-manager versions
or lockfiles; the nested SDK revision/integrity and override are retained in the
build receipt. The marker is scoped to those two child commands only.
Only the generated `dist` is
copied into the isolated installed package. A digest receipt prevents overwriting
or silently adopting existing output. Matrix SDK is rebuilt before Element.

Builds use Node 26 from
`/nix/store/dwgyq8kcsd74g5v7il5wng4vjivjyy87-nodejs-26-npm-12/bin/node`;
`env.sh` exposes that path after the sandbox and Python bin directories. Exact
resolved tools/versions and Call provenance are saved in `element-build-tools.json`
and `element-call-build.json`. `build-element --resume` retains partial state and
requires lock, package and workspace inputs to match the pinned Element commit;
it refuses a completed trusted build. It neither changes lockfiles nor modifies
the original Element checkout. `up` creates ops (allowlisted, bot power 0) and elsewhere
(not allowlisted, bot power 100), joins all accounts, then starts the gateway.
Alice logs into real Element and creates an encrypted direct room through its
real Matrix client. `01-dm.png` and `rooms.json` record that room.

Wait for the DM gateway INFO summary with at least 100 published and zero cleared
(`evidence/gateway.log`). With Synapse `rc_message: {per_second: 4, burst_count: 30}`,
confirm at least one real M_LIMIT_EXCEEDED retry under the publisher's 5/s pacing.
Save that observation in the journey report; a lack of retry evidence is a failed
acceptance condition. Then:

```bash
h dump-state --room dm --label dm-initial
h browser --step autocomplete-status
h browser --step run-status
h dump-state --room ops --label ops-initial
h dump-state --room elsewhere --label elsewhere-initial
h browser --step ops-empty
```

Inspect both initial group dumps: neither may contain a non-empty bot description.
Confirm the gateway logged one ops permission INFO with need 50 / have 0. The
browser asserts both built-in `/status` and Hermes `/status@hermes` autocomplete
rows, chooses the qualified row, verifies its sent body/mentions/structured command,
waits for the status reply including room ID, then asserts unqualified `/status`
opens Element's built-in error dialog without sending another status message.
Evidence: `02-autocomplete-status.png`, `03-status-reply.png`, `sent-status.json`,
`04-builtin-status.png`, `05-ops-no-power.png`, and provider counts.

```bash
h set-bot-power --room ops --level 50
h browser --step ops-granted
h dump-state --room ops --label ops-granted
h bob-command --room ops --text /status
h skill add
h browser --step reload-added
h skill remove
h browser --step reload-removed
h dump-state --room dm --label dm-removed
h conformance
```

The power-change trigger must publish without restart (`06-ops-granted.png`). Bob
is observed for 20 seconds with no bot reply and no model requests (`bob-denied.json`).
Reload steps send through the browser, require a prompt skill reply, wait for the
probe's presence/removal in synced state, and check autocomplete (`07-reload-*.png`).
`conformance` imports the actual pinned Element parser, room resolver, enabled status
built-in and content builder. Every nonempty bot event must parse; blanked events
must return null, status must require qualification and resolve to body `/status`.
It consumes the actual dumps listed above, never a fixture manifest.

```bash
h restart-gateway --disable
h browser --step disabled
h dump-state --room dm --label disabled
h dump-state --room ops --label ops-disabled
h conformance
h verify-element
h down
```

The disabled browser step waits for every bot description in both rooms to become
`{}`; conformance checks both disabled dumps. Browser checks no Hermes rows
and Element's unknown-command dialog for `/status@hermes` (`08-disabled.png`).
Evidence remains in `$SB/evidence`; the journey runner mirrors it into its `e2e/`
output. Do not claim E2E completion from offline selftest output.

If Element/bot E2EE blocks login or status, report the failure first. The approved
fallback is an unencrypted DM created through the local Matrix API, with that
limitation explicitly recorded. Do not silently weaken encryption.

`down` stops only owned groups/container and retains evidence and runtime state.
`down --purge` requires an explicit user request and removes runtime/build/browser
state while retaining evidence. A partial bootstrap retains receipts and credentials;
inspect private state before any explicit purge, rather than adopting other data.

## Offline validation

The task's serialized `t2-gate.sh` runs shell syntax checks, isolated Python compile,
`selftest`, Windows footgun checks and Node syntax validation. `selftest` starts no
services and covers configuration output, standard padded state keys, dump assertion
failures, ownership refusal, receipt identity mismatches, private file I/O, nested
secret redaction, artifact digest changes, exact dependency selection and rejection
of archive integrity mismatches. Browser and Element runtime assertions
are intentionally separate from that offline gate.
