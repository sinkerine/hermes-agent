# Matrix MSC4274 gallery inbound: plan (revision 2)

| | |
|---|---|
| Status | **Approved by the user (revision 2, 2026-11-09).** Revision 1's "approved by the user (2026-11-09)" line was history; this approval is current. |
| Base | `refs/heads/main` @ `db722732ad1e7002cd06fc3c813a109626466d59` |
| Branch | `matrix-gallery-inbound` (revision 1 named `feat/matrix-gallery-inbound`, which was wrong) |
| Worktree | `/home/sinkerine/.paseo/worktrees/15zkmyhp/matrix-gallery-inbound` |
| Delivery | **Merge into the default branch (`main`) after every gate in §5 passes and the consolidated review is OK** (user decision, revision 2). This replaces revision 1's "branch-only" wording. There is no push, no deploy, no live message and no production-state change. The main checkout `/home/sinkerine/dev/hermes-agent` is never edited by hand; the merge is a git ref operation done by the orchestrator. |
| Scope | Hermes Matrix adapter, its tests, stand-in and docs. The Element Web sender is read-only reference; it gets no cross-project edits. |

## 0. Why revision 2 exists (discrepancy)

The handoff expected a fresh design. HEAD instead already holds a preserved implementation:
four commits on the base, which are kept and not rewritten.

| Commit | Content |
|---|---|
| `87bdd356dc` | Revision 1 of this plan |
| `111f21e268` | T1: `adapter_gallery.py`, `adapter_media.py`, adapter wiring, unit tests, stand-in media route, E2E journey, user docs |
| `4141702a82` | Root `AGENTS.md` project-tree trim. This is unrelated to galleries; see D11. |
| `5a3fa2591a` | E2E fake-model `supports_vision` fix |

The worktree has no surviving receipts: no `.venv`, no `.hermes-sandbox/` and no run evidence.
Earlier pass claims therefore come from memory only and count for nothing. **Every gate in §5 must
be re-run from scratch on the final HEAD.** Revision 2 does three things:

1. Re-verifies the preserved code against base, the Element sender source and mautrix.
2. Records the current decisions (D1–D11).
3. Adds a small verification delta (T1).

No production-code change is planned. The only exception is a defect that T1's new tests expose.

## 1. Verified findings

The base adapter is the same as on the deployed `agent-sachi` build `f54c98a42e`, which is a
sibling of base off `ab24977e9f`. That build differs only in a skill and in `AGENTS.md`, so the
live drop applies to base.

| # | Finding | Where |
|---|---|---|
| F1 | **Base drops galleries silently.** `_on_room_message` routes only `m.image/m.audio/m.video/m.file` and `m.text/m.notice`. Anything else falls through with no log. Live evidence (sanitized): `m.room.message`, `msgtype: dm.filament.gallery`, `body: "what images do you see?"`, two plain `m.image` `image/png` MXC items, key set `{body, itemtypes, m.mentions, msgtype}`. | base `adapter.py:1948-1951` |
| F2 | Each media event calls `handle_message` once, so N separate events make N turns. | base `adapter.py:2181-2186` |
| F3 | `MessageEvent` carries the parallel lists `media_urls`, `media_types` and `media_filenames`. The busy merge (`extend_media`) preserves them. | `gateway/platforms/event.py`; `gateway/session_envelope.py` |
| F4 | `_resolve_message_context` is the one mention/thread/room-allowlist seam. It returns the mention-stripped body. Read receipt and thread mark run once. | `adapter.py:1956-2023` |
| F5 | **Sender allowlisting (`MATRIX_ALLOWED_USERS`) is enforced by the gateway runner after the adapter dispatches:** silent drop in groups, pairing code in DMs. The adapter checks system/bridge senders, ignore patterns and the room allowlist before routing. | `adapter.py:1896-1918`; `gateway/authz_mixin.py:707`; `gateway/run_inbound.py:80-81` |
| F6 | The edit check (`rel_type == m.replace`) runs before msgtype routing, so gallery edits are ignored. | `adapter.py:1937-1938` |
| F7 | The runner routes each attachment by its own MIME. Audio inside a PHOTO/VIDEO event goes through STT. | `gateway/run_inbound.py`, `gateway/run.py` |
| F8 | `adapter.py` is over the 2000-line target under a `FILE_LINES` waiver and may only shrink. Base: 3054 lines. HEAD: 3047 lines. | `scripts/code_health/config.py` |
| F9 | **mautrix 0.21.1** (locked, `mautrix[encryption]`) keeps unknown content keys such as `itemtypes` in `SerializableAttrs.unrecognized_` and returns them from `serialize()`. Synced events and Megolm-decrypted events share that deserializer, so encrypted-room galleries reach the same `source_content`. | `mautrix/types/util/serializable_attrs.py` (`_dict_to_attrs`, `_attrs_to_dict`) at tag v0.21.1 |
| F10 | **Element sender wire shape** (read-only, `element-web` @ `d698eb223e`; the `puny-cow` copy is byte-identical). Real names: `GALLERY_MSGTYPE`, `STABLE_GALLERY_MSGTYPE`, `GalleryItemContent`, `GalleryEventContent`, `createGalleryEventContent` (there is no `GalleryContent`/`createGalleryContent`). Details follow the table. | `apps/web/src/utils/MediaCaptionContent.ts`; `ContentMessages.ts:833-923`; `utils/Reply.ts:52-91`; `utils/messages.ts:46,122` |
| F11 | `main` itself fails `scripts/check` agents-md: root `AGENTS.md` is 12,307 chars against the 12,000 cap after `db722732ad`. Commit `4141702a82` trims it to 11,937. | `scripts/ci/check_agents_md_size.py:24` |
| F12 | The default test environment (`pm.testenv`, `[all]`) **excludes the `matrix` extra**, and so do CI's test lanes. Matrix-extra tests skip unless `HERMES_PYTHON` points at an environment built with `--extra matrix`. `scripts/run_tests.sh --fail-on-skip` turns any skip into a failure. | `pyproject.toml` `all = [...]` comment; `pm/testenv.py:19`; `.github/workflows/tests.yml:75`; `scripts/run_tests_parallel.py:1108,1345` |
| F13 | Type checking (`ty`) runs advisory-only in CI (`--exit-zero` diff report). There is no Python coverage gate. Docs CI runs `check_doc_links.py` and a Docusaurus build on website changes. | `.github/workflows/lint.yml:86`; `docs-site-checks.yml` |

F10 in detail:

- Outgoing galleries have 2–60 image/video items. One file falls back to a normal single-media
  event, with the caption in `body` and the name in `filename`.
- Each item is `{itemtype, body: file.name, info: {...w, h, thumbnail_info, thumbnail_url |
  thumbnail_file, mimetype, size} (video adds duration), url}` for plain media, or
  `{..., file: {v:"v2", key{alg,ext,k,key_ops,kty}, iv, hashes{sha256}, url}}` for encrypted
  media. **Items carry no `filename` field.**
- Event content is `{msgtype: "dm.filament.gallery", body: caption-or-"", [format,
  formatted_body], itemtypes, m.mentions}` (`attachMentions` always runs), plus `m.relates_to`.
  The relation is either a thread (`rel_type: m.thread`, `event_id`, `is_falling_back`,
  `m.in_reply_to`) or a reply mixin. **The body never contains a reply fallback.**
- On receipt Element accepts both `dm.filament.gallery` and `m.gallery`, with itemtypes from
  image/video/audio/file.

## 2. Goal and acceptance criteria

**Goal.** An inbound Matrix gallery (`dm.filament.gallery` or `m.gallery`) becomes exactly **one**
`MessageEvent` and **one** agent turn. That event carries the shared caption and every loadable
item, in sender order. Room, thread, reply, mention, authorization and dedup semantics match the
existing single-media path.

Each criterion is proven by a test named in §4 or §6.

- **AC1 – Routing.** Both msgtypes reach the gallery handler, and only after the existing checks
  (self, bridge, ignore-pattern, room-allow, dedup, startup-grace, `m.replace`, `m.notice`,
  interactive-UI). A gallery edit is ignored. A duplicate event ID dispatches once. Other unknown
  msgtypes are still dropped.
- **AC2 – Gate once, before any download.** `_resolve_message_context` is awaited exactly once,
  with the caption and the gallery content, so `m.mentions.user_ids` and `formatted_body` pills
  count. A gated-out gallery makes **zero** `download_media` calls and no dispatch. A gallery in a
  thread where the bot participates needs no mention, exactly as for text.
- **AC3 – Item validation.** An item is accepted only with a `url` or `file.url` starting with
  `mxc://`. These are skipped and noted: `info.size` over `MATRIX_MAX_MEDIA_BYTES`, an encrypted
  item missing `key.k`, `hashes.sha256` or `iv`, an unknown or non-string itemtype, and a
  non-dict item. Items past 60 are counted and not loaded. No single bad item drops the caption or
  the turn.
- **AC4 – Ordered fetch.** Accepted items are downloaded, decrypted when encrypted and cached
  sequentially in item order. Images go to the image cache (non-image bytes are rejected), video to
  `cache_video_from_bytes_async`, audio to the audio cache and files to the document cache.
- **AC5 – One dispatch.** Exactly one `handle_message` per gallery, with:
  - `text`: the caption after the reply-fallback and mention strip (`""` when uncaptioned), plus
    the D7 note when any item is missing.
  - The parallel media lists in item order.
  - `message_type` per D1.
  - `raw_message is content`.
  - `reply_to_*` and `source.thread_id` set exactly as for single media.
- **AC6 – All items fail.** The caption plus the note is dispatched once, with `TEXT` and no media.
- **AC7 – Authorization parity (D9).** Sender allowlisting is unchanged. An unauthorized sender's
  gallery never produces a model turn or a group reply; DMs follow the existing pairing flow.
- **AC8 – Sender compatibility.** Every Element `createGalleryEventContent` shape (F10) and the
  sanitized live shape (F1) is delivered as one turn with all items in order. This covers plain,
  encrypted, uncaptioned, HTML-captioned, threaded and reply cases. It holds for content that went
  through mautrix's real deserializer as well as for a plain dict.
- **AC9 – No regressions.** Single-media and text behaviour is unchanged, apart from D3. All
  existing tests show no new failures against base (§5.2 C8).
- **AC10 – Real E2E.** The §6 journey passes, not skipped, against the real `hermes gateway run`
  child, the Matrix stand-in and the recording fake model.

## 3. Design

### 3.1 Decisions

D1–D8 are preserved and re-confirmed by the user in revision 2. D9–D11 are new in revision 2.

| # | Decision |
|---|---|
| D1 | `message_type` is computed over **delivered** items: `PHOTO` if any image was cached, else `VIDEO` if any video, else `DOCUMENT` if any audio or file, else `TEXT`. |
| D2 | `m.audio` and `m.file` items are accepted and cached with their real MIME. Audio inside a PHOTO/VIDEO gallery goes through STT (core per-item routing, F7). This is documented, not changed. |
| D3 | Captioned single media uses `content.filename` (a non-empty string) for its cache name, falling back to `body`. This also covers Element's one-file fallback. Text blanking and routing are unchanged. |
| D4 | Single-media video stays in the document cache under its original name. Gallery videos use the video cache, and the original name goes in `media_filenames`. |
| D5 | `MAX_GALLERY_ITEMS = 60` is a fixed constant. A 1-item gallery is accepted. The per-item cap is `MATRIX_MAX_MEDIA_BYTES`, checked against `info.size` before download and against the actual length after. No new env var, config key or aggregate cap. |
| D6 | A failed item has no HTTP-URL fallback; it is skipped and noted. |
| D7 | A model-facing English note goes after the caption, separated by a blank line (or is the whole text when there is no caption). Filenames pass through `clean_attachment_filename`. Forms: `[Gallery: K of N items could not be loaded: item P 'name' (reason), …]`, `[Gallery: none of the N items could be loaded: …]`, `[Gallery: the message contained no media items]`, plus the suffix `; M more items beyond the 60-item gallery limit were not loaded`. |
| D8 | Captions get no `!cmd` bang normalization, matching single media. A leading `/` keeps today's media semantics. |
| D9 | **Authorization parity with single media** (user decision). There is no pre-download allowlist check: the runner decides after dispatch. Accepted consequence: an unauthorized member of an allowed room can make Hermes fetch up to 60 items (each ≤ `MATRIX_MAX_MEDIA_BYTES`) before rejection. A pre-download check through `_is_authorized_source` is a possible follow-up. |
| D10 | **Delivery: merge into `main` after all gates and review** (user decision). This supersedes revision 1's branch-only wording. Process in §4.3. |
| D11 | **Keep `4141702a82` as its own commit** (user decision). It is an unrelated guard fix for F11 (a project-tree trim in root `AGENTS.md`, with no rule text changed), reviewed separately from the feature diff. If `main` or `matrix-command-descriptions` changes `AGENTS.md` first, the rebase takes the landed text, re-trims only if still over 12,000 chars, and reruns `scripts/check --only agents-md`. |

### 3.2 Module boundaries (preserved, unchanged)

| Module | Owns |
|---|---|
| `plugins/platforms/matrix/adapter_media.py` (new) | The shared inbound fetch primitive: MXC download, encrypted-file key validation, `decrypt_attachment`, `IMAGE_EXT_BY_MIME`, and the MSC2530 filename preference. |
| `plugins/platforms/matrix/adapter_gallery.py` (new) | The pure parser, per-item caching, D1 type choice, the D7 note, and dispatch orchestration. |
| `plugins/platforms/matrix/adapter.py` (edit) | One routing branch, `_download_and_cache_media` delegating to `fetch_media_bytes` (signature and WARNINGs unchanged), and D3 in `_handle_media_message`. Net −7 lines. |

Siblings late-import the facade only inside functions (`ContentURI`), so there is no module-level
cycle. `gateway/`, `run_agent.py` and `MessageEvent` are untouched (plugins never touch core).

### 3.3 Interfaces (exact, as implemented)

```python
# plugins/platforms/matrix/adapter_media.py
IMAGE_EXT_BY_MIME: dict[str, str]  # image/jpeg .jpg, image/png .png, image/gif .gif, image/webp .webp
def encrypted_file_keys(encrypted_file: dict) -> Optional[tuple[str, str, str]]: ...
    # (key.k | bare key, hashes.sha256, iv), or None when any is missing or not a non-empty str
async def fetch_media_bytes(client: Any, url: str, event_id: str,
                            encrypted_file: Optional[dict]) -> Optional[bytes]: ...
    # download_media(ContentURI(url)) and decrypt_attachment when encrypted.
    # None on empty data or missing keys (existing WARNING). Transport/crypto errors propagate.
def media_filename(content: dict) -> str: ...  # non-empty str filename, else str body, else ""

# plugins/platforms/matrix/adapter_gallery.py
GALLERY_MSGTYPES: frozenset[str] = frozenset({"dm.filament.gallery", "m.gallery"})
MAX_GALLERY_ITEMS = 60
@dataclass(frozen=True) class GalleryItem:  position: int; itemtype: str; kind: MessageType; filename: str
                                            mimetype: str; url: str; encrypted_file: Optional[dict]
@dataclass(frozen=True) class GallerySkip:  position: int; filename: str; reason: str
@dataclass(frozen=True) class GalleryParse: items: list[GalleryItem]; skipped: list[GallerySkip]
                                            total: int; over_limit: int
def parse_gallery(content: dict, *, max_media_bytes: int) -> GalleryParse: ...
async def cache_gallery_item(data: bytes, item: GalleryItem) -> str: ...  # ValueError: non-image bytes
def gallery_message_type(kinds: Sequence[MessageType]) -> MessageType: ...
def missing_items_note(total: int, missing: Sequence[GallerySkip], over_limit: int) -> str: ...
async def _fetch_gallery_items(adapter, event_id, items) -> tuple[list[str], list[str], list[str],
                                                                  list[MessageType], list[GallerySkip]]: ...
async def handle_gallery_message(adapter: "MatrixAdapter", room_id: str, sender: str, event_id: str,
                                 source_content: dict, relates_to: dict, msgtype: str) -> None: ...

# tests/fakes/platforms/matrix_standin.py (test seam)
MatrixStandin.put_media(data: bytes, *, media_id: str | None = None) -> str  # "mxc://standin.local/<id>"
MatrixStandin.post_content(room_id: str, sender: str, content: dict) -> str  # event_id; post() delegates
# Route GET /_matrix/client/v1/media/download/{server_name}/{media_id}: Bearer-checked raw bytes,
# recorded as "media_download"; unknown media or foreign server gives 404 M_NOT_FOUND.
```

Existing seams consumed, with signatures unchanged: `_resolve_message_context`,
`_build_inbound_event(..., ctx=, media_msgtype=)`, `handle_message`, `_max_media_bytes`,
`_client`, `gateway.platforms.base.cache_{image,video,audio,document}_from_bytes_async`, and
`clean_attachment_filename`.

### 3.4 Data flow

```
_on_room_message: existing checks (F5, F6) → msgtype ∈ GALLERY_MSGTYPES
 → handle_gallery_message
   1. caption = body if str else ""
   2. ctx = _resolve_message_context(... caption, content, relates_to) ; None → return   (no parse, no I/O)
   3. parsed = parse_gallery(content, max_media_bytes)                                 (pure; items[:60])
   4. for item in parsed.items, sequentially:
        fetch_media_bytes → None: "download failed"
                          → len > max: size reason
        cache_gallery_item → ValueError for PHOTO: "not a valid image"
        any other Exception → WARNING with exc_info, "download or decryption failed"
   5. ev = _build_inbound_event(..., ctx=ctx, message_type=D1, media_*=lists, media_msgtype=msgtype)
   6. ev.text += D7 note (missing items sorted by position)
   7. INFO "Matrix gallery %s: %d/%d items cached"
   8. await handle_message(ev)                                                         (exactly once)
```

The runner then authorizes per D9, canonicalizes identity, applies busy-queue/media merge and
routes each attachment by MIME. All of that is unchanged core.

### 3.5 Error handling and conventions

- Per-item failures are contained at the item boundary. `BaseException` (cancellation)
  propagates. Gate, build and dispatch errors propagate as they do for single media.
- Logs never contain keys, hashes or caption text.
- CC ≤ 20 and nesting ≤ 6 per function. `adapter.py` must not exceed its base size.
- User docs: one paragraph in `website/docs/user-guide/messaging/matrix.md` (preserved). The
  zh-Hans translation is not updated; there is no translation-sync rule.
- Behaviour that parity carries over: profile scope and identity are handled by the base
  `handle_message` seam, exactly as for single media.

## 4. Tasks

### 4.1 T1: sender-compatibility and authorization verification delta (single wave)

| Field | Value |
|---|---|
| Goal | Prove AC7 and AC8 with Element-exact and live-shape fixtures, the real mautrix deserializer and an unauthorized-sender E2E step. Fix only defects these tests expose. |
| Dependencies | None for code. C0 (environment) must finish before any test command. |
| Owned files | `tests/plugins/platforms/matrix/test_gallery_sender_compat.py` (**new**); `tests/e2e/core/platforms/test_matrix_gallery_inbound.py` (edit). Conditional: `tests/fakes/platforms/matrix_standin.py` plus `tests/e2e/core/platforms/test_matrix_standin_smoke.py`, only if a non-member sender's events are not delivered, to add `MatrixStandin.add_member(room_id: str, user_id: str) -> None` with an honesty test. Conditional: `plugins/platforms/matrix/adapter_gallery.py`, only for a defect proven red by a new test. |
| Consumes | §3.3 interfaces; `mautrix.types` deserializer and `mautrix.crypto.attachments.encrypt_attachment`; `make_adapter` from `test_inbound_media_filename.py`; `MatrixDriver`, `GatewayUnderTest`, `Director`, `FakeLLMServer`. |
| Produces | The test modules below. No new production interface. Possibly `add_member`. |
| Unit command | `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh --fail-on-skip tests/plugins/platforms/matrix/test_gallery_sender_compat.py -v` |
| External dependencies | None at runtime. Element source is read only to transcribe fixtures. |

`test_gallery_sender_compat.py` holds a fixture builder `element_gallery(...)`. It transcribes
`createGalleryEventContent`, `withMediaCaption`, `attachMentions`, `attachRelation` and
`addReplyToMessageContent` at element-web `d698eb223e`, with a provenance comment naming the files
and lines from F10. The item builder emits `body = file name`, **no `filename`**, `info` with
`mimetype`/`size`/`w`/`h` and optional thumbnail fields, and either `url` or
`file{v,key,iv,hashes,url}` from a real `encrypt_attachment`. Every case drives
`_on_room_message` and asserts exactly one `handle_message`.

1. **Live shape (F1):** exact key set `{msgtype, body, itemtypes, m.mentions}`, two plain PNG
   items, caption `what images do you see?`.
   - `text` equals the caption.
   - `media_types == ["image/png"] * 2`.
   - `media_filenames` equal the item bodies.
   - Cached bytes are in order.
   - `message_type == PHOTO`.
2. **Encrypted:** 2 and 60 items with `thumbnail_file`. All are decrypted to their original bytes,
   in order.
3. **Uncaptioned:**
   - DM (`body: ""`, `m.mentions: {}`): `text == ""`, no note.
   - Group with require-mention and no mention: zero downloads, no dispatch (documented behaviour).
   - Group in a bot-participated thread: dispatched without a mention.
4. **HTML caption:** `format`/`formatted_body` with a bot pill and `m.mentions.user_ids`, in a
   group with require-mention. Dispatched with the mention stripped.
5. **Thread mixin:** `{rel_type: m.thread, event_id: $root, is_falling_back: true,
   m.in_reply_to}` gives `source.thread_id == $root`. **Reply mixin** without a thread gives
   `reply_to_message_id`, and the body has no fallback to strip.
6. **`m.gallery` alias** of case 1: identical result.
7. **mautrix seam:** a raw `m.room.message` event dict for case 1 (and case 2 with 2 items) goes
   through mautrix's real event deserializer (`mautrix.types.Event.deserialize` or the class the
   client's sync uses; the implementer confirms which) and then to `_on_room_message`. `itemtypes`
   survive and the same single dispatch results. This proves F9 for synced and decrypted events.

All mautrix imports use `pytest.importorskip(..., reason="requires the matrix extra")` and must
execute under `--fail-on-skip`.

The E2E edits are in §6: step 1 items in Element shape, the stranger step and the live-shape step.

### 4.2 Conflicts and shared resources

- T1 is the only implementation task, so there is no file overlap within this feature.
- **`matrix-command-descriptions`** (separate worktree, clean at base) will probably edit
  `plugins/platforms/matrix/adapter.py`, possibly `website/docs/user-guide/messaging/matrix.md`,
  and it will also hit F11 on `AGENTS.md`. Its files and environment stay isolated in its own
  worktree and `.venv`. Whichever branch lands second must:
  1. Rebase onto the new `main`.
  2. Resolve conflicts in the routing branch and import line of `adapter.py`, and in
     `AGENTS.md` per D11.
  3. Rerun C7, since the `adapter.py` `FILE_LINES` cap is judged on the combined file, plus C1–C6.
- **Mutable resources:**
  - `.venv/`: the PM build is serialized under `<git-dir>/e2e-feature-build.lock` and never
    rebuilt while tests run.
  - `test_durations.json`: written at the worktree root by the runner, so test commands in this
    worktree run serially.
  - `.hermes-sandbox/<run>/` and `--basetemp` directories are per run.
  - The PM tool store and uv cache are shared, PM- and uv-locked, append-only caches.
  - Stand-in and fake-LLM ports are ephemeral loopback ports.

### 4.3 Order

1. C0 environment.
2. T1.
3. All §5 gates on HEAD.
4. **Consolidated review** of the whole feature diff `db722732ad..HEAD`, with `4141702a82`
   reviewed separately as D11. No early review is planned, because no consequential uncertainty
   remains.
5. Delivery (D10):
   1. Rebase onto the current local `main`, or merge if `main` is unchanged.
   2. Rerun C1–C7 after any non-trivial rebase.
   3. Rerun C6 if the routing, stand-in or gateway paths changed.
   4. Rerun C8 as a comparison.
   5. Fast-forward `main`.
   6. No push, no deploy.

## 5. Test and verification

### 5.1 Environment, discovery and prerequisites

- **Runner:** always `scripts/run_tests.sh`, never bare pytest. It runs file-granular
  subprocesses with `TZ=UTC` and `LANG=C.UTF-8`, credentials unset and a temp `HERMES_HOME`. When
  not activated, it honors an explicit `HERMES_PYTHON` that has pytest. Discovery: the positional
  paths below, or `tests/` by default. Runner option `--fail-on-skip` makes any skip a failure.
  Bare pytest flags such as `-v` and `-rs` pass through.
- **C0, the interpreter with the matrix extra.** Prerequisite: the worktree has no `.venv`, and
  the system Python lacks pytest, mautrix and olm (orchestrator preflight). It runs once, under
  the build lock, and the output must not already exist:

  ```bash
  cd /home/sinkerine/.paseo/worktrees/15zkmyhp/matrix-gallery-inbound
  flock /home/sinkerine/dev/hermes-agent/.git/worktrees/matrix-gallery-inbound/e2e-feature-build.lock \
    python -m pm.build_env --source . --out .venv --group dev --group test --extra matrix
  .venv/bin/python -c "import pytest, mautrix, olm, mautrix.crypto.attachments; assert mautrix.__version__ == '0.21.1'; print('matrix extra OK')"
  ```

  This needs network only for wheels missing from the uv cache. If `olm` fails to import, or the
  build fails (for example a native-library problem on NixOS), **report it as a blocker. Never
  fall back to skipping.** Never pip/uv-mutate `.venv`; rebuild it fresh instead (stop its
  processes, remove `.venv`, run the build again).
- **Isolated state for every command below:** run `export
  HERMES_HOME="$PWD/.hermes-sandbox/<run>/home"
  HERMES_RUNTIME_DIR="$PWD/.hermes-sandbox/<run>/runtime"` with a fresh `<run>` per check.
  `.hermes-sandbox/` is git-ignored. Never touch `~/.hermes`.
- **Generated inputs:** nothing is checked in. PNG and JPEG byte literals live in the tests.
  Ciphertext and keys come from `encrypt_attachment` at test time. Video bytes are arbitrary.
- **Commands that write or rewrite files:**
  - `pm.build_env` creates `.venv/`.
  - The runner rewrites `test_durations.json`.
  - `--basetemp` **wipes and recreates** its directory (create its parent first).
  - `git archive` in C8 writes `.hermes-sandbox/base-tree/`.
  - No check uses `--fix` or a formatter. `scripts/check` is read-only.
- **Excluded by instruction:** all desktop E2E (core, visual regression, update,
  installer/lifecycle). Real Synapse with an Element browser is not part of this feature's gate
  (no live accounts).

### 5.2 Required check inventory (all on the final HEAD)

| # | Check | Command | Pass rule |
|---|---|---|---|
| C1 | Feature unit tests | `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh --fail-on-skip tests/plugins/platforms/matrix/test_gallery_inbound.py tests/plugins/platforms/matrix/test_gallery_sender_compat.py tests/plugins/platforms/matrix/test_inbound_media_filename.py -v` | All pass, 0 skipped |
| C2 | Matrix plugin suite | `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/plugins/platforms/matrix/ -rs` | All pass. No skip reason mentions the matrix extra or mautrix/olm. |
| C3 | Matrix adapter regressions | `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/gateway/test_matrix*.py -rs` | Same as C2 |
| C4 | Plugin contract | `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/gateway/test_shared_platform_boilerplate.py` | All pass |
| C5 | Stand-in honesty and existing Matrix journeys | `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh --fail-on-skip tests/e2e/core/platforms/test_matrix_standin_smoke.py tests/e2e/core/platforms/test_matrix_project_routing.py tests/e2e/core/platforms/test_matrix_project_memory.py -v` | All pass, 0 skipped |
| C6 | Feature E2E (§6) | `mkdir -p "$PWD/.hermes-sandbox/e2e" && HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh --fail-on-skip tests/e2e/core/platforms/test_matrix_gallery_inbound.py -v --basetemp="$PWD/.hermes-sandbox/e2e/journey"` | Pass, 0 skipped. Evidence is kept under the basetemp directory. |
| C7 | Lint and ratchets (ruff, code-health incl. `FILE_LINES`/BLE001/HX, agents-md, blob-size, guards) | `.venv/bin/python scripts/check --base db722732ad1e7002cd06fc3c813a109626466d59`; after a rebase: `--base main` | Exit 0 |
| C8 | Broad regression versus base | HEAD: `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/gateway/ tests/plugins/ -rs`. For each file that fails on HEAD and exists at base: `mkdir -p .hermes-sandbox/base-tree && git archive db722732ad \| tar -x -C .hermes-sandbox/base-tree` (once), then `(cd .hermes-sandbox/base-tree && HERMES_PYTHON="$PWD/../../.venv/bin/python" scripts/run_tests.sh <file>)` | No failure on HEAD that is absent at base. Any failure shared with base is listed with its test IDs as a **pre-existing blocker** in the receipt. A failure in a HEAD-only file is a feature failure. If the base tree cannot run a file, report that as a blocker; don't presume the failure is pre-existing. |
| C9 | Type check (advisory, as in CI) | `.venv/bin/ty check plugins/platforms/matrix/adapter_gallery.py plugins/platforms/matrix/adapter_media.py plugins/platforms/matrix/adapter.py`, and the same `adapter.py` run in `.hermes-sandbox/base-tree` | No diagnostics in the new files. `adapter.py` count not above base. Advisory: report, don't block. |
| C10 | Docs | `python3 website/scripts/check_doc_links.py` | Exit 0. The Docusaurus build is left to CI `docs-site-checks`; it is not run locally because the change is one plain paragraph with no links or MDX, and it would need `npm ci`. |

Not applicable: coverage thresholds (none exist for Python), visual/screenshot checks (no UI
change; desktop is excluded) and build artifacts (nothing new is packaged).

### 5.3 Isolated smoke and sender compatibility

- **S1 smoke:** C1 inside a fresh `.hermes-sandbox/smoke/{home,runtime}`. The receipt records the
  `HERMES_PYTHON` path, `mautrix.__version__` and the pass/skip counts.
- **Sender compatibility:** covered by C1 (`test_gallery_sender_compat.py`, §4.1 cases 1–7) and by
  C6 steps 1 and 6.
  - The fixtures are transcriptions with provenance, not imports. That keeps the boundary clean:
    nothing in element-web is executed or written.
  - If the sender mission later captures a real event, it can be added as one more fixture row,
    as a follow-up.

## 6. E2E journey (`tests/e2e/core/platforms/test_matrix_gallery_inbound.py`)

Setup:

- Markers `platforms("linux")` and `spawns_gateway_lookalike`; `importorskip("mautrix",
  reason="requires the matrix extra")`.
- The real `hermes gateway run` runs through `GatewayUnderTest` on pytest's `tmp_path`, with the
  `MatrixDriver` stand-in and `FakeLLMServer` + `Director`.
- Config: `agent.image_input_mode: native`, `model.supports_vision: true`.
- Env: driver defaults (`MATRIX_ALLOWED_USERS = admin,guest`) with `MATRIX_REQUIRE_MENTION=true`
  in the 3-member group room `!alpha`.
- Call `arm_readiness()` before `start()`.
- `finally` stops the gateway and the stand-in.
- There are no live accounts and no real provider.

Item shape in all steps is Element's (F10): `body` is the name, there is no `filename`, and
`info{mimetype,size}` is present.

| Step | Action | Expected outcome |
|---|---|---|
| 1 | Seed plain PNG **A**, video bytes **V** (`body: clip.mp4`), and `encrypt_attachment(PNG B)` as ciphertext **Bc** (Element `file` shape). **C** is unseeded and returns 404. Post a `dm.filament.gallery` with `body: "[in:gallery] compare these"`, `m.mentions` naming the bot, and items A, V, B, C. | `director.turns["gallery"] == 1`. The last user message has `image_url` parts equal to `[data(A), data(B)]`, in that order. The text has the caption, a video note naming `clip.mp4`, and `[Gallery: 1 of 4 items could not be loaded: item 4 …`. `media_download` order is a, v, b, c. One bot reply. |
| 2 | Uncaptioned **`m.gallery`** (`body: ""`, bot in `m.mentions`) with 2 images, in a thread on step 1's event, using Element's thread mixin (`is_falling_back`, `m.in_reply_to`). | One turn. Two images in order. No caption text and no note. The reply carries `m.thread` with the step 1 event ID. |
| 3 | In order: (a) an **unmentioned** gallery with fresh media D, E; (b) an `m.replace` edit of step 1 whose `m.new_content` is a gallery; (c) **new:** a mentioned gallery from `@stranger:standin.local`, who is not in `MATRIX_ALLOWED_USERS`, with fresh media S1, S2 (join the stranger to `!alpha` with `add_member` if needed); (d) a mentioned text barrier `[in:after] done?`. | The barrier reply arrives. `director.turns` has no `gated`, `edited` or `stranger` entry. There are zero `media_download` calls for D and E. The room has no bot reply to the stranger. Per D9 the S1/S2 download count is **recorded, not asserted**. |
| 4 | A mentioned gallery `[in:allfail]` with 2 unseeded items. | One turn. User text is the caption, a blank line, then `[Gallery: none of the 2 items could be loaded: …`. No `image_url` parts. One reply. |
| 5 | **New, live shape (F1):** content with exactly `{msgtype: dm.filament.gallery, body: "[in:live] what images do you see?", itemtypes: [2 × plain m.image image/png], m.mentions: {user_ids: [bot]}}`. | One turn. Two `image_url` parts in item order. The text contains the caption and has no note. One reply. |
| 6 | Teardown checks. | No call faulted with `M_UNRECOGNIZED`. `bot_messages(other_room_id) == []`. The gateway is stopped in `finally`. Evidence (gateway log, state DB, cached media) is kept under `--basetemp`. |

Restart is not exercised: the feature adds no persisted state, and the existing restart journeys
in C5 cover the session machinery.

## 7. Risks and notes

- **Sequential download** of up to 60 × the media cap can make one turn slow. This is accepted
  because order is required. A concurrency cap is a possible follow-up.
- **D9 amplification** (F5) is an accepted risk. A pre-download `_is_authorized_source` check is
  the possible follow-up.
- **D2:** audio inside an image or video gallery is transcribed (core routing).
- **Environment risk:** the `matrix` extra needs `python-olm`. A failed build or import blocks
  C1–C6. That is reported, never skipped.
- The integration with `matrix-command-descriptions` is described in §4.2.
