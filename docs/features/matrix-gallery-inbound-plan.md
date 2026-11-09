# Matrix MSC4274 gallery inbound — implementation plan

Status: **approved by the user (2026-11-09), decisions D1–D8 as recommended** · Base: `refs/heads/main` @ `db722732ad` · Branch:
`feat/matrix-gallery-inbound` · Worktree: `/home/sinkerine/.paseo/worktrees/15zkmyhp/matrix-gallery-inbound`

Delivery is **branch-only**: nothing is merged into or edited in the main checkout
(`/home/sinkerine/dev/hermes-agent`). Element-web is out of scope. The sender side (media upload
as galleries) is a separate mission in a separate workspace, and this plan makes no cross-project
edits.

## 0. Verified findings (base `db722732ad`)

| # | Claim | Verified at |
|---|---|---|
| F1 | Gallery msgtypes are silently dropped. `_on_room_message` routes only `m.image/m.audio/m.video/m.file` and `m.text/m.notice`; any other msgtype falls off the end with no log. | `plugins/platforms/matrix/adapter.py:1948-1951` |
| F2 | Sending media as separate events gives one turn per event. Each media event calls `handle_message` once. | `adapter.py:2181-2186` |
| F3 | `MessageEvent` already carries parallel `media_urls` / `media_types` / `media_filenames` lists, and the session envelope and busy merge (`extend_media`) preserve them. | `gateway/platforms/event.py:95-139,154-164`; `gateway/session_envelope.py:37` |
| F4 | `_resolve_message_context` is the single permission/mention/reply/thread seam. It reads `m.mentions.user_ids` and `formatted_body` through `_content_mentions_bot`, strips the mention, sets thread or synthetic-thread IDs, builds `source`, and sends the read receipt and thread mark once. | `adapter.py:1953-2020,2900-2904` |
| F5 | The single-media path gates **before** downloading, and an invariant test pins this. | `adapter.py:2155-2168`; `tests/gateway/test_matrix_media_gate_before_download.py` |
| F6 | `_download_and_cache_media` downloads, validates encrypted metadata (`key.k`, `hashes.sha256`, `iv`) and decrypts. It names files from `body`, which can be a caption (MSC2530). `m.video` goes to the **document** cache with the body as its name. | `adapter.py:2201-2232` |
| F7 | The runner routes each attachment by its own MIME before falling back to the message-level type. A `video/*` entry inside a PHOTO event is still treated as video (path note), and `image/*` entries attach as images. `audio/*` inside a PHOTO/VIDEO event enters STT; inside AUDIO/DOCUMENT it becomes an audio-file note. | `gateway/run_inbound.py:1431-1449`; `gateway/run.py:2494-2511` |
| F8 | `adapter.py` is 3054 lines, over the 2000-line target, under a `# health: allow FILE_LINES` waiver. The ratchet caps it at its base size, so it may only shrink and new behaviour must go into a `<stem>_<topic>.py` sibling. | `scripts/code_health/config.py:22-36`; root `AGENTS.md` § Facade + siblings |
| F9 | The Matrix stand-in advertises spec `v1.11`, so mautrix 0.21.1 `download_media` uses the **authenticated** `GET /_matrix/client/v1/media/download/{server}/{media_id}`. The stand-in has no media route and no way to post arbitrary content; unknown routes return 404 `M_UNRECOGNIZED`. | `tests/fakes/platforms/matrix_standin.py:139-163,213`; mautrix `media_repository.py:184-196` |
| F10 | The edit check (`rel_type == m.replace`, which skips edits including `m.new_content` galleries) runs before msgtype routing, so gallery edits are already ignored. | `adapter.py:1937-1938` |

## 1. Goal and acceptance criteria

**Goal.** An inbound Matrix MSC4274 gallery (`msgtype: dm.filament.gallery`, stable alias
`m.gallery`) becomes **one** `MessageEvent` and **one** agent turn. The event carries the shared
caption and the items' cached media in item order.

**Acceptance criteria** (each is proven by a test named in §4/§5):

- **AC1 – Routing.** Both msgtypes route to the gallery handler only after the existing self /
  bridge / ignore-pattern / room-allow / dedup / startup-grace / `m.replace` / `m.notice` /
  interactive-UI checks. A gallery `m.replace` edit is still ignored. Other unknown msgtypes are
  still dropped.
- **AC2 – Gate once, before any download.** `_resolve_message_context` runs exactly once per
  gallery, with the shared caption as `body` and the gallery `content` (so `m.mentions.user_ids`
  and `formatted_body` pills count). A gated-out gallery performs **zero** `download_media` calls
  and no dispatch.
- **AC3 – Per-item validation.** An item is accepted only if it has a plain `url` or a `file.url`,
  either one starting with `mxc://`. A declared `info.size` above `_max_media_bytes` is rejected
  before download, as is an encrypted `file` missing `key.k`, `hashes.sha256` or `iv`. An
  unknown itemtype, a non-dict item, or an item past position 60 is skipped. Every skipped or
  failed item appears in a missing-items note, and one bad item never drops the caption or the
  turn.
- **AC4 – Ordered download/cache.** Accepted items are downloaded, decrypted and cached
  **sequentially in item order**, using the existing download/decrypt helper. Images go to the
  image cache, videos to `cache_video_from_bytes_async`, audio to the audio cache, and files to
  the document cache.
- **AC5 – One dispatch.** Exactly one `handle_message` call per gallery:
  - `text` is the caption after the existing reply-fallback strip and mention strip (`""` when
    uncaptioned), plus the missing-items note when any item is missing.
  - `media_urls` / `media_types` / `media_filenames` are parallel lists in item order.
  - `message_type` follows D1.
  - `raw_message` is the gallery `content`.
  - `reply_to_*` and thread fields are set exactly as for other media.
- **AC6 – All items fail.** The caption plus the note is still dispatched, once, with no media.
- **AC7 – No regressions.** Single-media and text behaviour and all existing tests are unchanged.
  The one intentional single-media change is D3: a captioned single media event uses
  `content.filename` for its cache name and extension.
- **AC8 – Real E2E.** A feature journey drives the real `hermes gateway run` child against the
  Matrix stand-in and the recording fake LLM, and shows one model request per gallery with the
  images in order.

## 2. Decisions

Accepted by the user at approval as written.

| # | Topic | Recommended default |
|---|---|---|
| D1 | `message_type` | Computed over **delivered** items: `PHOTO` if any image was cached, else `VIDEO` if any video was cached, else `DOCUMENT` if any audio/file item was cached, else `TEXT` (all failed). This matches "PHOTO if image present else VIDEO" for every image/video gallery. The two extra rungs stop a zero-media event from claiming PHOTO (which would trigger busy-path photo queueing and an empty vision placeholder) and keep an audio-only gallery as a file note instead of STT (F7). |
| D2 | Tolerated `m.audio` / `m.file` items | Accepted and cached (audio cache / document cache) with their real MIME. Known consequence (F7): an audio item inside a PHOTO/VIDEO gallery goes through STT like a voice note, because per-item routing is core behaviour, which this plugin change does not alter. |
| D3 | Single-media filename | `_handle_media_message` passes `content.filename` (non-empty str) or else `body` as the cache-name source. This fixes captioned `m.file`/`m.audio`/`m.video` being cached under the caption with no extension. Captions, text blanking (`_is_bare_media_filename`) and routing stay as they are. |
| D4 | Single-media video cache | **Unchanged**: it stays in the document cache under its original name, which preserves current paths (AC7). Gallery videos use `cache_video_from_bytes_async` and carry the original name in `media_filenames`, because video cache names are random. |
| D5 | Gallery limits | Fixed constant `MAX_GALLERY_ITEMS = 60`, matching the sender's 2..60. A gallery with 1 item is accepted. Items past 60 are counted in the note, not downloaded. There is no new env var or config key and no aggregate byte cap. The per-item cap is `MATRIX_MAX_MEDIA_BYTES`, checked against `info.size` before download and against the real byte length after. |
| D6 | Failed items | No HTTP-URL fallback (unlike single unencrypted media). A failed item is skipped and noted. Authenticated-media URLs aren't usable by tools anyway. |
| D7 | Missing-items note | A model-facing English line appended after the caption with a blank line between (or the whole text when uncaptioned). Not i18n, like the runner's other attachment notes. Examples: `[Gallery: 2 of 5 items could not be loaded: item 2 'b.png' (larger than the 104857600-byte media limit), item 4 'c.mov' (download failed)]`, `[Gallery: none of the 3 items could be loaded: …]`, `[Gallery: the message contained no media items]`, and a `; N more items beyond the 60-item gallery limit were not loaded` suffix. Filenames pass through `clean_attachment_filename`. |
| D8 | Commands in captions | Parity with single media: no `!cmd` bang normalization. A caption starting with `/` keeps today's media semantics (the mention-gate bypass in `_resolve_message_context`, and `MessageEvent.is_command()`). |

## 3. Design

### 3.1 Module boundaries

`adapter.py` is a facade over the file-lines cap (F8), so behaviour goes in two new siblings and
the facade only wires them, ending with a net **negative** line count.

| Module | Owns |
|---|---|
| `plugins/platforms/matrix/adapter_media.py` (new) | The inbound media fetch primitive shared by single media and galleries: encrypted-metadata validation, MXC download, decryption, the image-extension map, and filename preference. Moved out of `_download_and_cache_media`, which keeps its signature and its caching tail. |
| `plugins/platforms/matrix/adapter_gallery.py` (new) | The MSC4274 parser (pure), per-item caching, message-type choice, note formatting and the dispatch orchestration. |
| `plugins/platforms/matrix/adapter.py` (edit) | One routing branch in `_on_room_message`. `_download_and_cache_media` calls `adapter_media.fetch_media_bytes` and uses `IMAGE_EXT_BY_MIME`, with D3 applied in `_handle_media_message`. |

Siblings late-import the facade inside functions where they need its names (`ContentURI` stub
fallback). There is no module-level cycle. `adapter.py` imports `adapter_gallery` at module level
(gallery imports nothing from the facade at import time).

### 3.2 Interfaces (exact signatures)

```python
# plugins/platforms/matrix/adapter_media.py
IMAGE_EXT_BY_MIME: dict[str, str]  # {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"} (moved)

def encrypted_file_keys(encrypted_file: dict) -> Optional[tuple[str, str, str]]:
    """(key.k, hashes.sha256, iv) of an MSC encrypted-file dict, or None when any is missing.
    Accepts ``key`` as a JWK dict or a bare string, exactly as today."""

async def fetch_media_bytes(client: Any, url: str, event_id: str,
                            encrypted_file: Optional[dict]) -> Optional[bytes]:
    """``client.download_media(ContentURI(url))`` then ``decrypt_attachment`` when *encrypted_file*
    is given. None when the download is empty or decryption metadata is missing (logs the existing
    WARNING). Transport and decryption errors propagate to the caller."""

def media_filename(content: dict) -> str:
    """MSC2530: ``content['filename']`` when a non-empty str, else ``content['body']`` (or "")."""
```

```python
# plugins/platforms/matrix/adapter_gallery.py
GALLERY_MSGTYPES: frozenset[str] = frozenset({"dm.filament.gallery", "m.gallery"})
MAX_GALLERY_ITEMS = 60

@dataclass(frozen=True)
class GalleryItem:
    position: int                    # 1-based index in content["itemtypes"]
    itemtype: str                    # m.image | m.video | m.audio | m.file
    kind: MessageType                # PHOTO | VIDEO | AUDIO | DOCUMENT
    filename: str                    # clean_attachment_filename(media_filename(item)); "" if unusable
    mimetype: str                    # info.mimetype, else the kind default (image/png, video/mp4, audio/ogg, application/octet-stream)
    url: str                         # mxc:// URI (plain url or file.url)
    encrypted_file: Optional[dict]   # the item's ``file`` dict when encrypted

@dataclass(frozen=True)
class GallerySkip:
    position: int
    filename: str
    reason: str                      # short model-facing reason (D7)

@dataclass(frozen=True)
class GalleryParse:
    items: list[GalleryItem]         # accepted, in item order
    skipped: list[GallerySkip]       # rejected before download, in item order
    total: int                       # len(itemtypes) when it is a list, else 0
    over_limit: int                  # items past MAX_GALLERY_ITEMS (neither parsed nor listed)

def parse_gallery(content: dict, *, max_media_bytes: int) -> GalleryParse: ...
async def cache_gallery_item(data: bytes, item: GalleryItem) -> str: ...   # cache path; ValueError for non-image bytes
def gallery_message_type(kinds: Sequence[MessageType]) -> MessageType: ... # D1 over delivered kinds
def missing_items_note(total: int, missing: Sequence[GallerySkip], over_limit: int) -> str: ...  # "" when nothing missing
async def handle_gallery_message(adapter: "MatrixAdapter", room_id: str, sender: str, event_id: str,
                                 source_content: dict, relates_to: dict, msgtype: str) -> None: ...
```

Existing seams this consumes (unchanged signatures): `MatrixAdapter._resolve_message_context`,
`_build_inbound_event(..., ctx=..., media_msgtype=...)`, `handle_message`, `_max_media_bytes`,
`_client`, plus `gateway.platforms.base.cache_{image,video,audio,document}_from_bytes_async` and
`gateway.platforms.event.clean_attachment_filename`.

### 3.3 Data flow

```
_on_room_message (all existing checks, F10)
  └─ msgtype in GALLERY_MSGTYPES → adapter_gallery.handle_gallery_message(self, room_id, sender, event_id,
                                                                          source_content, relates_to, msgtype)
       1. caption = body if isinstance(body, str) else ""
       2. ctx = await adapter._resolve_message_context(room_id, sender, event_id, caption, source_content, relates_to)
          └─ None → return                                   # AC2: no parse side effects, no download
       3. parsed = parse_gallery(source_content, max_media_bytes=adapter._max_media_bytes)   # pure
       4. for item in parsed.items (sequential, in order):
            data = await fetch_media_bytes(adapter._client, item.url, event_id, item.encrypted_file)
            None                       → miss "download failed"
            len(data) > max            → miss "larger than the N-byte media limit"
            path = await cache_gallery_item(data, item)
              ValueError (image check) → miss "not a valid image"
            other Exception            → logger.warning(..., exc_info=True); miss "download or decryption failed"
            ok → urls/types/names/kinds append (item.mimetype, item.filename)
       5. ev = await adapter._build_inbound_event(room_id, sender, event_id, caption, source_content, relates_to,
               ctx=ctx, message_type=gallery_message_type(kinds), media_urls=urls, media_types=types,
               media_filenames=names, media_msgtype=msgtype)
          # reply-fallback strip + reply_to_* fields; gallery msgtype is never a "bare filename"
       6. note = missing_items_note(parsed.total, sorted(parsed.skipped + misses, key=position), parsed.over_limit)
          ev.text = f"{ev.text}\n\n{note}" if ev.text and note else (ev.text or note)
       7. await adapter.handle_message(ev)                    # exactly once
```

`cache_gallery_item` routing:
- PHOTO: `cache_image_from_bytes_async(data, ext=IMAGE_EXT_BY_MIME.get(mime, ".jpg"))`.
- VIDEO: `cache_video_from_bytes_async(data, ext=<filename suffix> or ".mp4")`.
- AUDIO: `cache_audio_from_bytes_async(data, ext=<filename suffix> or ".ogg")`.
- DOCUMENT: `cache_document_from_bytes_async(data, item.filename or "document")`.

Suffixes are lower-cased, and only a short alphanumeric `.xxx` suffix is accepted.

`parse_gallery` per item, in order:
1. If not a dict, or past position 60 (counted in `over_limit` only), skip.
2. An itemtype outside `_ITEM_KINDS` → "unsupported item type".
3. `url` = plain `url`, else `file.url`. Missing → "no media URL"; not `mxc://` → "not a Matrix
   media URL".
4. `info` is a dict or else `{}`. If `int(info.size)` > max → size reason. A non-numeric size
   counts as unknown, as in the single-media path.
5. An encrypted item with `encrypted_file_keys(file) is None` → "missing decryption keys".

### 3.4 Error handling and conventions

- Per-item failures never escape the loop. Exceptions are caught at that single item boundary
  with `logger.warning(..., exc_info=True)` (BLE001-compliant). Cancellation (`BaseException`)
  propagates. Gate, build and dispatch errors propagate exactly as for single media.
- Logs never include decryption keys, hashes or caption text. Item positions, itemtype and
  `_redact_url_for_log`-safe MXC IDs are allowed. One INFO line per gallery:
  `Matrix gallery %s: %d/%d items cached`.
- No new env vars, config keys or core changes. `gateway/`, `run_agent.py` and `MessageEvent`
  are untouched (plugins-never-touch-core).
- Function CC ≤ 20 and nesting ≤ 6. `handle_gallery_message` delegates the item loop to a
  private `_fetch_gallery_items(adapter, event_id, items) -> tuple[list, list, list, list,
  list[GallerySkip]]` to stay small.
- `adapter.py` ends at ≤ 3054 lines (F8). The fetch extraction saves more lines than the routing
  branch adds; the implementer confirms with `python scripts/check`.
- User docs: one short paragraph in `website/docs/user-guide/messaging/matrix.md` (next to the
  media-limit paragraph, ~line 331) covering inbound galleries as one turn, the 60-item limit and
  missing-item notes.

## 4. Tasks

One cohesive task; splitting the wiring from its tests would be artificial.

| Field | T1 — gallery inbound + tests + E2E |
|---|---|
| Wave | 1 (only) |
| Dependencies | None. Shared build resource: the disposable PM env `.venv` with `--extra matrix` (§5.1), built once before T1 runs; only one builder at a time. |
| Owned files | `plugins/platforms/matrix/adapter_media.py` (new), `plugins/platforms/matrix/adapter_gallery.py` (new), `plugins/platforms/matrix/adapter.py` (edit), `tests/plugins/platforms/matrix/test_gallery_inbound.py` (new), `tests/plugins/platforms/matrix/test_inbound_media_filename.py` (new), `tests/fakes/platforms/matrix_standin.py` (edit: media route, `put_media`, `post_content`), `tests/e2e/core/platforms/test_matrix_standin_smoke.py` (edit: stand-in media-route honesty test), `tests/e2e/core/platforms/test_matrix_gallery_inbound.py` (new), `website/docs/user-guide/messaging/matrix.md` (edit) |
| Consumes | §3.2 existing seams; `mautrix.crypto.attachments.{decrypt,encrypt}_attachment` (matrix extra); `GatewayUnderTest`, `Director`, `FakeLLMServer`, `MatrixDriver` |
| Produces | §3.2 interfaces; `MatrixStandin.put_media(data: bytes, *, media_id: str \| None = None) -> str` (returns `mxc://standin.local/<id>`); `MatrixStandin.post_content(room_id: str, sender: str, content: dict) -> str` (returns `event_id`; `post()` delegates to it); stand-in route `GET /_matrix/client/v1/media/download/{server_name}/{media_id}` (Bearer-checked, raw bytes, recorded as method `media_download`, unknown media → 404 `M_NOT_FOUND`) |
| Unit test command | `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/plugins/platforms/matrix/test_gallery_inbound.py tests/plugins/platforms/matrix/test_inbound_media_filename.py -v` |
| External dependencies | None at runtime. Tests need the `matrix` extra (mautrix 0.21.1 + crypto) for encryption tests and the E2E. No network, real accounts or providers. |

### 4.1 Unit coverage (`test_gallery_inbound.py`)

Build the adapter as `tests/gateway/test_matrix_media_gate_before_download.py` does. Fake
`_client.download_media` with an `AsyncMock` keyed by MXC URI, run the real cache helpers under
the autouse temp `HERMES_HOME`, and capture `handle_message`.

1. **Parsing** (`parse_gallery`, pure): plain `url`; `file.url`; non-MXC `url` and non-MXC
   `file.url` rejected; `info.size` over the cap skipped; non-numeric size treated as unknown;
   missing URL; unknown itemtype; non-dict item; `itemtypes` missing or not a list → `total == 0`;
   61 items → 60 parsed, `over_limit == 1`; encrypted item missing `k`/`iv`/`sha256` skipped;
   `filename` preferred over `body`; unsafe filename cleaned.
2. **Routing**: `dm.filament.gallery` and **stable alias `m.gallery`** both dispatch;
   an `m.replace` gallery edit with `m.new_content` → no dispatch, no download; an unknown
   msgtype → no dispatch; self sender → no dispatch; a duplicate event ID → one dispatch.
3. **Gating before download**: group room with require-mention on.
   - Unmentioned → zero `download_media` awaits and zero dispatch.
   - `m.mentions.user_ids` with an empty caption → dispatch.
   - `formatted_body` pill → dispatch.
   - `@hermes:example.org look` → text `look`.
   - `_resolve_message_context` awaited exactly once (spy).
4. **Encryption** (`pytest.importorskip("mautrix.crypto.attachments", reason="requires the
   matrix extra")`): a real `encrypt_attachment` item is decrypted and cached with the original
   bytes. A bad `hashes.sha256` → that item is noted and the rest still delivered.
5. **Mixed order**: image, video, file, image → `media_urls` order matches.
   - `media_types == [image/png, video/mp4, application/pdf, image/jpeg]`; `media_filenames`
     hold the original names.
   - The video path is under `get_video_cache_dir()`; `message_type == PHOTO`.
   - `download_media` call order equals item order.
6. **Empty caption**: `body == ""`, all items OK → `text == ""`, no note.
7. **Partial failure**: one `download_media` returns `None`, one raises, one is oversized →
   the note lists exactly those positions and names, the caption is preserved, and the others are
   delivered.
8. **All failure**: `message_type == TEXT`, no media, `text == caption + "\n\n" + note`, one
   dispatch. An uncaptioned all-failed gallery → `text == note`.
9. **ONE dispatch**: a 5-item gallery → `handle_message.await_count == 1`.
10. **Reply/thread**:
    - Reply: `m.in_reply_to` with a reply-fallback body → `reply_to_message_id`,
      `reply_to_text` and `reply_to_author_id` set, and the caption stripped of the fallback.
    - Thread: `m.thread` → `source.thread_id == root`.
    - `raw_message is content`.
11. **Message type (D1)**: video-only → VIDEO; file-only → DOCUMENT; image-failed with a video
    OK → VIDEO.

`test_inbound_media_filename.py` (D3, single media):
- `m.file` with `body: "see attached"` and `filename: "report.pdf"` → cached name ends with
  `report.pdf` and `text == "see attached"`.
- `m.audio` with a caption and `filename: "a.m4a"` → `.m4a` extension.
- With no `filename`, the body is used as before.

## 5. Test and verification

### 5.1 Environment discovery and prerequisites

- **Runner:** always `scripts/run_tests.sh` (never bare pytest). It unsets credentials, sets
  `TZ=UTC`/`LANG=C.UTF-8`, points `HERMES_HOME` at a temp dir and isolates each file in its own
  subprocess. Preflight already ran `scripts/run_tests.sh
  tests/plugins/platforms/matrix/test_event_size.py -q` → 2 passed, with
  `HERMES_HOME=$PWD/.hermes-sandbox/preflight/home` and
  `HERMES_RUNTIME_DIR=$PWD/.hermes-sandbox/preflight/runtime`.
- **Interpreter with the matrix extra** (a shared build resource; build once, fresh; if `.venv`
  exists, stop its processes and remove it deliberately first):
  `python -m pm.build_env --source . --out .venv --group dev --group test --extra matrix`.
  Then pass `HERMES_PYTHON="$PWD/.venv/bin/python"` to every run below so that matrix-extra
  tests **execute, not skip**. The runner's default test env may omit the extra.
- **Isolated state:** export `HERMES_HOME=$PWD/.hermes-sandbox/<run>/home` and
  `HERMES_RUNTIME_DIR=$PWD/.hermes-sandbox/<run>/runtime` (`.hermes-sandbox/` is git-ignored).
  Never touch `~/.hermes`. The E2E creates its own HOME under pytest `tmp_path`.
- **Generated inputs:** none checked in. Test images are tiny PNG/JPEG byte literals built in the
  test (they pass `_looks_like_image`). Video bytes are arbitrary (the video cache doesn't
  validate content). Encrypted items are produced at test time by `encrypt_attachment`.
- **Resource conflicts:** the stand-in and fake LLM bind ephemeral loopback ports, so there are
  no fixed ports. The E2E spawns a gateway process group, so don't run two E2E journeys with the
  same `tmp_path`. `.venv` must not be rebuilt while tests run.
- **Skipped by instruction:** all desktop E2E suites (desktop core, visual regression, update,
  installer/lifecycle). Real Synapse/Element browser E2E is out of scope (Element-web excluded).

### 5.2 Required check inventory

| # | Check | Command |
|---|---|---|
| C1 | New unit tests | `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/plugins/platforms/matrix/test_gallery_inbound.py tests/plugins/platforms/matrix/test_inbound_media_filename.py -v` |
| C2 | Matrix plugin tests (incl. interactive UI) | `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/plugins/platforms/matrix/` |
| C3 | Existing Matrix adapter regressions (single media, mention, voice, metadata, gate-before-download) | `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/gateway/test_matrix.py tests/gateway/test_matrix_media_gate_before_download.py tests/gateway/test_matrix_media_filename.py tests/gateway/test_matrix_mention.py tests/gateway/test_matrix_message_event_metadata.py tests/gateway/test_matrix_voice.py tests/gateway/test_matrix_secret_capture.py tests/gateway/test_matrix_project_context_isolation.py` |
| C4 | Shared-boilerplate / plugin contract | `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/gateway/test_shared_platform_boilerplate.py` |
| C5 | Stand-in honesty + existing Matrix journeys (stand-in edited) | `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/e2e/core/platforms/test_matrix_standin_smoke.py tests/e2e/core/platforms/test_matrix_project_routing.py tests/e2e/core/platforms/test_matrix_project_memory.py -v` |
| C6 | Feature E2E (§6) | `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/e2e/core/platforms/test_matrix_gallery_inbound.py -v` (must PASS, not skip) |
| C7 | Lint / ratchets (ruff, code-health incl. FILE_LINES on `adapter.py`, BLE001, HX rules) | `python scripts/check` |
| C8 | Broad regression before handoff | `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/gateway/ tests/plugins/` |

### 5.3 Isolated smoke and sender compatibility

- **Smoke:** C1 inside the isolated `HERMES_HOME`/`HERMES_RUNTIME_DIR`, repeating the preflight
  pattern.
- **Sender compatibility:** the media-upload sender lives in a separate workspace, so there is no
  cross-project edit or import. `test_gallery_inbound.py` carries a fixture that copies the
  requested wire shape exactly (`dm.filament.gallery`, `body`, `format` + `formatted_body`,
  `itemtypes[]` with `itemtype/body/filename/info{mimetype,size,w,h,duration,thumbnail_info,
  thumbnail_url|thumbnail_file}`, `url` or `file{url,key{k},iv,hashes{sha256},v:"v2"}`,
  `m.mentions`, `m.relates_to`) for 2 and 60 items, plain and encrypted. Thumbnails are ignored
  inbound. If the sender workspace later publishes a captured event, adding it as one more
  fixture row is a follow-up for that mission.

## 6. E2E journey

`tests/e2e/core/platforms/test_matrix_gallery_inbound.py`:
- Markers: `platforms("linux")`, `spawns_gateway_lookalike`; `importorskip("mautrix", reason=
  "requires the matrix extra")`.
- Runs the real `hermes gateway run` child through `GatewayUnderTest` on pytest `tmp_path`, with
  `MatrixDriver`'s stand-in and `FakeLLMServer` + `Director`.
- Config merge: `agent.image_input_mode: native` (images arrive as recorded `image_url` data
  URLs).
- Env: driver defaults with `MATRIX_REQUIRE_MENTION=true`, so the room is a 3-member group and
  gating is real.
- `driver.arm_readiness()` is called before `gateway.start()`.
- `finally` stops the gateway and the stand-in. No live accounts and no real provider.

| Step | Action | Expected outcome |
|---|---|---|
| 1 | Seed `put_media`: PNG **A**, MP4 bytes **V**, `encrypt_attachment(PNG B)` → ciphertext **Bc**. Image **C** is left unseeded (404). Post a `dm.filament.gallery` with `body: "[in:gallery] compare these"`, `m.mentions.user_ids: [bot]`, and items A (`url`), V (`url`, `filename: clip.mp4`), B (`file`), C (`url`). | Exactly **one** model turn for `gallery` (`director.turns["gallery"] == 1`). The last user message has `image_url` parts `== [dataurl(A), dataurl(B)]` in that order. The text contains the caption, a video note naming `clip.mp4`, and `[Gallery: 1 of 4 items could not be loaded: item 4 …]`. `media_download` calls hit A, V, Bc, C in order. One bot reply lands in the room. |
| 2 | Post an uncaptioned **`m.gallery`** (`body: ""`, `m.mentions` bot) with two images in a thread (`m.relates_to` `m.thread` on step 1's event). | One model turn (token `none`, scripted). Two image parts in order; the user text has no caption and no note. The reply carries `m.thread` → the step-1 event ID. |
| 3 | Post an **unmentioned** gallery with fresh media IDs D, E and `[in:gated]`. Then post an `m.replace` edit of step 1 whose `m.new_content` is a gallery with `[in:edited]`. Then post a mentioned text barrier `[in:after] done?`. | The barrier reply arrives. `director.turns` has no `gated` or `edited` entries, and there are zero `media_download` calls for D/E. |
| 4 | Post a mentioned gallery `[in:allfail]` whose 2 items are both unseeded. | One model turn whose user text is the caption + `[Gallery: none of the 2 items could be loaded: …]` with no `image_url` parts. One reply. |
| 5 | Teardown checks. | No stand-in call faulted with `M_UNRECOGNIZED` (every route the adapter used exists). `driver.standin.bot_messages(driver.other_room_id) == []`. The gateway is stopped in `finally`. |

Restart is not exercised: the feature adds no persisted state (galleries become ordinary events
in the existing session machinery, already covered by the restart journeys in C5).

## 7. Risks and notes for implementers

- **Sequential downloads** of up to 60 × 100 MB can make one turn slow. That is accepted, since
  ordering is required and it matches single-media cost per item. A concurrency cap would be a
  follow-up if needed.
- **D2 STT side effect** (audio inside an image/video gallery) is core routing (F7). It is
  documented, not changed here.
- Keep `_download_and_cache_media`'s signature and its existing WARNINGs unchanged:
  `tests/gateway/test_matrix_media_gate_before_download.py` and others patch it as an instance
  attribute.
- Commit on `feat/matrix-gallery-inbound` only; rebase onto `main` before any later merge (root
  rule), which is outside this mission's branch-only delivery.
