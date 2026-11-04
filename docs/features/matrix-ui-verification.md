# Matrix interactive UI verification

The deterministic encrypted native-browser journey passed its recorded assertions. **The required live quality gate FAILED, the 30-request budget is exhausted, and this branch is not verified complete or merge-ready.** Continuing live evaluation requires a user decision to authorize extra budget or explicitly waive the failed criterion; neither follows automatically from the deterministic result.

This is a redacted verification record, not raw logs. Evidence links below resolve into the ignored local sandbox and are **not durable branch artifacts**: they will not accompany a clone or commit. They identify retained private receipts for local review, without reproducing credentials, nonces, action values or private event identifiers. Historical worker reports describe service ownership at their respective timestamps; at final closure all sandbox services are stopped and evidence is retained. No merge, push or archive was performed.

## Implementation and review record

The eight tasks received these review counts (review rounds do not imply that every acceptance gate passed):

| Task | Scope | Reviews | Commit from `git log` |
|---|---|---:|---|
| T1 | Protocol port and vendored conformance contract | 2 | `b002761558` |
| T2 | Scoped plugin delivery and toolset restrictions | 2 | `076fcf70a8` |
| T3 | Matrix rendering helpers | 1 | `896e7c9c18` |
| T4 | Complete fallbacks and Matrix content | 4 | `d038ecd366` |
| T5 | Durable actions and submission validation | 1 | `067373d668` |
| T6 | `present_ui` producer and encrypted media | 2 | `b760a2e1db` |
| T7 | Action validation and durable acknowledgement | 3 | `7ba92d31f4` |
| T8 | Disposable Matrix/Element E2E harness | 3 | `ff45a40ab0` |

Plan commit: `6a87a7b3c3`. Subsequent harness repair: `44d5cba1ba` (sandbox Web build and deferred tool journey). Baseline: `a81a7148316f`. The committed harness transition is independently recorded in the private journey provenance; it changed five files under `scripts/sandbox/matrix_interactive_ui/`. See the [approved plan and review log](matrix-ui-plan.md).

## Deterministic encrypted journey

The [journey report](../../.hermes-sandbox/matrix-ui/evidence/journey.md) and [private acceptance audit](../../.hermes-sandbox/matrix-ui/evidence/journey-acceptance-private.json) establish:

- Eight actual delivered cards (six root, two thread), four accepted action turns, and exactly one continuation per accepted action. Local selection and duplicate replay produced no additional continuation. Duplicate rejection survived gateway restart; stale and inactive probes produced the expected notices without a turn.
- Eight invalid-root tool errors each followed by successful correction and actual delivery. The earlier direct-tool lookup failure is retained separately and excluded from these eight pairs.
- Native encrypted Element interaction: Chinese/English graphs, sortable table, chart data view, forms, and carousel navigation with three actual encrypted uploads and decrypted images. Deterministic fact/joke prompts remained text-only.
- Submitted status became visible in **227 ms** for the root form and **346 ms** for the thread form, both within eight seconds and before the model reply. These are observed samples, not a latency distribution or service guarantee.
- Thread card, action and continuation share the thread root. The status edit uses top-level `m.replace` targeting the original card, with no relation in `m.new_content`. The threaded form visibly updates: this proves the relation chain, not that the edit itself carries `m.thread`.
- **68 journey screenshots: 67 nonfailure captures and one retained failure capture.** The separate smoke screenshot is excluded. These are captures, not 68 independent passing cases. Inventory: 39 historical captures (including the failure), 15 native text-scroll captures, and 14 graph overflow/list captures.
- The audit reproducibly passes **41 assertions**, including persisted correction/delivery pairs, action/timing/relation evidence and screenshot accounting.

Historical harness blockers (direct invocation of a deferred tool, and forced Labs configuration disabling Bob's switch) were repaired and rerun; failed receipts remain available. The native journey used real Element UI, with no renderer mock or settings/DOM mutation bypass. Audit reproduction, using retained private evidence and the sandbox environment:

```bash
source scripts/sandbox/matrix_interactive_ui/env.sh
python "$SB/evidence/audit-journey.py"
```

## Bounded live Magpie evaluation: failed required gate

The [live result](../../.hermes-sandbox/matrix-ui/evidence/live-eval.json), [budget ledger](../../.hermes-sandbox/matrix-ui/evidence/live-budget.json), and [initial TLS-failure receipt](../../.hermes-sandbox/matrix-ui/evidence/live-eval-tls-failed.json) account for **30 reserved request slots: nine TLS-failed attempts plus 21 actual requests**. TLS failures consumed reserved budget; they are not successful model evaluations.

| Live cases | Actual requests | Recorded outcome |
|---|---:|---|
| Chinese graph, English graph, table | 9 | One valid card each after correction |
| Chart, form | 4 | One valid card each |
| Carousel | 3 | One valid card after correction; media unverified by evaluator |
| Forced invalid payload | 3 | Missing-root error, then `nodes[0].gap: bad_value`; **zero valid cards** |
| Fact, joke | 2 | Appropriate text-only answers, no UI tool |

Six ordinary UI cases produced valid cards. The forced-invalid case did **not** self-correct to a valid card within its three requests, so the required live criterion failed. An empty `failed_cases` list and a `completed` case status in the evaluator JSON do not override `forced_invalid_self_corrected: false`, zero valid cards, or inappropriate-use status for that case. This evaluation establishes protocol quality only, not live encrypted delivery or live image upload. No additional model calls are authorized by this report.

## Repository validation and attribution limits

The [full-suite log](../../.hermes-sandbox/matrix-ui/evidence/full-suite.log) reports **70,104 passed, 259 failed, 401 errors and 1,053 skipped**. The suite is not green. The [bounded triage](../../.hermes-sandbox/matrix-ui/evidence/check-triage.md) and [machine-readable triage](../../.hermes-sandbox/matrix-ui/evidence/check-triage.json) preserve per-file classification and exact rerun arguments. Only **eight redaction failures and six uv fixture errors** were reproduced on the baseline in the selected comparison; other failures remain unclassified as to baseline/branch causality. Environment candidates and unchanged source are not proof of pre-existing failures. Three timed-out files partially executed tests and remain unresolved.

A branch-specific stale import was identified in `tests/plugins/platforms/test_interactive_setup_reconfigure_gate.py`: the Matrix wizard moved from `adapter` to `setup`. Commit `49278dd1d5` repairs the import while preserving its assertions. The parent independently reran the repaired file and Matrix setup tests: **17 passed, zero failed**; evidence is `reconfigure-parent.log`.

The [focused feature log](../../.hermes-sandbox/matrix-ui/evidence/final-feature-tests.log) records **896 passed, six skipped, zero failed across 21 files**, before the final stale-import repair. A separate generic-seam rerun recorded 118 passed and six skipped. Neither result clears the full suite or constitutes verification of the later repair.

The [tracked snapshot check](../../.hermes-sandbox/matrix-ui/evidence/final-snapshot-check.log) passed **all 14 checks** for commit `49278dd1d5`, with zero blocking/advisory health findings. Recorded command:

```bash
source scripts/sandbox/matrix_interactive_ui/env.sh
python scripts/check --commit HEAD --base a81a7148316f
```

This certifies the recorded committed snapshot, not subsequent uncommitted changes. The populated-worktree check still has two scanner failures caused by sandbox dependency artifacts: ignored nested Git content is traversed by the temporary-path scanner, and dependency archives trigger the profile-archive scanner. No evidence was hidden or removed to obtain the supported snapshot result. Snapshot lint is not proof that the populated worktree is a clean Docker publication context.

The bounded triage and final setup-repair test receipts use `scripts/run_tests.sh` with a documented in-memory scratch/environment/cache override because the canonical runner drops sandbox variables through `env -i` and otherwise selects `/var/tmp`. Canonical interpreter selection, credential stripping, per-file isolation and selection flags were retained; no test behavior was patched. Selected comparison flags were `-j 2 --file-retries 0 --file-timeout 45 --tb=short`; the exact five-file selection, adapter source and replay commands are retained in the triage JSON. Those bounded comparison/repair runs are **not unmodified canonical-runner invocations**. The earlier focused and full-suite runs used the unmodified runner; although launched with asserted sandbox environment variables, its internal scratch behavior was discovered later. Do not claim those earlier executions stayed wholly inside the worktree. Do not rerun live evaluation or a full suite merely to reproduce this documentation.

## Remaining risks and limits

- Encrypted uploads can leave orphan media if later delivery fails; no transactional media cleanup guarantee is established.
- URL validation conservatively rejects some Unicode hostnames accepted by browser WHATWG parsing; parity is not exact.
- Legacy receipts without provable complete replay history remain unsafe and require a newly issued card. Recovery needs the durable ownership receipt database and matching identity/watermark; loss of that database is not recoverable from an arbitrary old Matrix event.
- The accepted crash window remains between durable acceptance and continuation dispatch. Durable deduplication does not guarantee exactly-once model execution across every crash.
- Optional overdue-client “Try again” behavior was not covered by controlled transport interruption. A successfully submitted form disables Submit; replay evidence is not that optional retry path.
- Carousel 1×1 PNGs prove encrypted transport/decryption/navigation, not useful image quality.
- Layered graphs overflow horizontally at both 1280 px and 360 px. Native scrolling reaches the final layer; the relationships list and vertically scrolled text view provide complete content. The full graph does not fit simultaneously at either width.
- Bob's historical flag-off fallback content is retained, but saved browser storage could not restore Bob's sign-in on reopening. New complete rendering captures use Alice's native text view and are not fresh Bob flag-off browser proof.
- A worker provisioned outside the sandbox into the real `~/.hermes` location. The external delta is unknown; no cleanup was performed. This prevents a claim that all worker activity remained sandbox-contained.

The live gate and unresolved full-suite failures remain open. Final setup-repair validation and tracked-snapshot checks pass. Any decision to spend more live budget or waive the live criterion must be explicit and user-owned; this report supplies no completion or merge approval.
