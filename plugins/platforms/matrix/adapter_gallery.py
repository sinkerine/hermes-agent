"""MSC4274 inbound galleries: one gated event with ordered, cached attachments."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

from gateway.platforms.event import MessageType, clean_attachment_filename
from .adapter_media import IMAGE_EXT_BY_MIME, encrypted_file_keys, fetch_media_bytes, media_filename

if TYPE_CHECKING:
    from .adapter import MatrixAdapter

logger = logging.getLogger(__name__)
GALLERY_MSGTYPES: frozenset[str] = frozenset({"dm.filament.gallery", "m.gallery"})
MAX_GALLERY_ITEMS = 60
_ITEM_KINDS = {
    "m.image": (MessageType.PHOTO, "image/png"),
    "m.video": (MessageType.VIDEO, "video/mp4"),
    "m.audio": (MessageType.AUDIO, "audio/ogg"),
    "m.file": (MessageType.DOCUMENT, "application/octet-stream"),
}


@dataclass(frozen=True)
class GalleryItem:
    position: int
    itemtype: str
    kind: MessageType
    filename: str
    mimetype: str
    url: str
    encrypted_file: Optional[dict]


@dataclass(frozen=True)
class GallerySkip:
    position: int
    filename: str
    reason: str


@dataclass(frozen=True)
class GalleryParse:
    items: list[GalleryItem]
    skipped: list[GallerySkip]
    total: int
    over_limit: int


def _size_reason(max_media_bytes: int) -> str:
    return f"larger than the {max_media_bytes}-byte media limit"


def _parse_item(item: object, position: int, max_media_bytes: int) -> GalleryItem | GallerySkip:
    if not isinstance(item, dict):
        return GallerySkip(position, "", "invalid media item")
    filename = clean_attachment_filename(media_filename(item))
    itemtype = item.get("itemtype")
    if not isinstance(itemtype, str) or itemtype not in _ITEM_KINDS:
        return GallerySkip(position, filename, "unsupported item type")
    encrypted = item.get("file")
    encrypted = encrypted if isinstance(encrypted, dict) else None
    url = item.get("url") or (encrypted.get("url") if encrypted is not None else "")
    if not url:
        return GallerySkip(position, filename, "no media URL")
    if not isinstance(url, str) or not url.startswith("mxc://"):
        return GallerySkip(position, filename, "not a Matrix media URL")
    info = item.get("info")
    info = info if isinstance(info, dict) else {}
    try:
        size = int(info.get("size") or 0)
    except (TypeError, ValueError, OverflowError):
        size = 0
    if size > max_media_bytes:
        return GallerySkip(position, filename, _size_reason(max_media_bytes))
    if encrypted is not None and encrypted_file_keys(encrypted) is None:
        return GallerySkip(position, filename, "missing decryption keys")
    kind, default_mime = _ITEM_KINDS[itemtype]
    mime = info.get("mimetype")
    mime = mime if isinstance(mime, str) and mime else default_mime
    return GalleryItem(position, itemtype, kind, filename, mime, url, encrypted)


def parse_gallery(content: dict, *, max_media_bytes: int) -> GalleryParse:
    raw = content.get("itemtypes")
    if not isinstance(raw, list):
        return GalleryParse([], [], 0, 0)
    items, skipped = [], []
    for position, value in enumerate(raw[:MAX_GALLERY_ITEMS], 1):
        parsed = _parse_item(value, position, max_media_bytes)
        (skipped if isinstance(parsed, GallerySkip) else items).append(parsed)
    return GalleryParse(items, skipped, len(raw), max(0, len(raw) - MAX_GALLERY_ITEMS))


async def cache_gallery_item(data: bytes, item: GalleryItem) -> str:
    from gateway.platforms.base import (
        cache_audio_from_bytes_async, cache_document_from_bytes_async,
        cache_image_from_bytes_async, cache_video_from_bytes_async,
    )

    if item.kind == MessageType.PHOTO:
        return await cache_image_from_bytes_async(data, ext=IMAGE_EXT_BY_MIME.get(item.mimetype, ".jpg"))
    if item.kind == MessageType.DOCUMENT:
        return await cache_document_from_bytes_async(data, item.filename or "document")
    suffix = Path(item.filename).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ".mp4" if item.kind == MessageType.VIDEO else ".ogg"
    cache = cache_video_from_bytes_async if item.kind == MessageType.VIDEO else cache_audio_from_bytes_async
    return await cache(data, ext=suffix)


def gallery_message_type(kinds: Sequence[MessageType]) -> MessageType:
    for kind in (MessageType.PHOTO, MessageType.VIDEO):
        if kind in kinds:
            return kind
    return MessageType.DOCUMENT if kinds else MessageType.TEXT


def missing_items_note(total: int, missing: Sequence[GallerySkip], over_limit: int) -> str:
    if total == 0:
        return "[Gallery: the message contained no media items]"
    count = len(missing) + over_limit
    if not count:
        return ""
    prefix = (f"none of the {total} items could be loaded" if count == total else
              f"{count} of {total} items could not be loaded")
    details = ", ".join(
        f"item {item.position}" + (f" '{item.filename}'" if item.filename else "") + f" ({item.reason})"
        for item in missing)
    note = f"[Gallery: {prefix}" + (f": {details}" if details else "")
    if over_limit:
        note += f"; {over_limit} more items beyond the {MAX_GALLERY_ITEMS}-item gallery limit were not loaded"
    return note + "]"


async def _fetch_gallery_items(
    adapter: MatrixAdapter, event_id: str, items: Sequence[GalleryItem],
) -> tuple[list[str], list[str], list[str], list[MessageType], list[GallerySkip]]:
    urls, types, names, kinds, missing = [], [], [], [], []
    for item in items:
        try:
            data = await fetch_media_bytes(adapter._client, item.url, event_id, item.encrypted_file)
            if data is None:
                missing.append(GallerySkip(item.position, item.filename, "download failed"))
                continue
            if len(data) > adapter._max_media_bytes:
                missing.append(GallerySkip(item.position, item.filename, _size_reason(adapter._max_media_bytes)))
                continue
            try:
                path = await cache_gallery_item(data, item)
            except ValueError:
                if item.kind != MessageType.PHOTO:
                    raise
                missing.append(GallerySkip(item.position, item.filename, "not a valid image"))
                continue
        except Exception:
            logger.warning("Matrix gallery %s item %d failed", event_id, item.position, exc_info=True)
            missing.append(GallerySkip(item.position, item.filename, "download or decryption failed"))
            continue
        urls.append(path)
        types.append(item.mimetype)
        names.append(item.filename)
        kinds.append(item.kind)
    return urls, types, names, kinds, missing


async def handle_gallery_message(adapter: MatrixAdapter, room_id: str, sender: str, event_id: str,
                                 source_content: dict, relates_to: dict, msgtype: str) -> None:
    body = source_content.get("body")
    caption = body if isinstance(body, str) else ""
    ctx = await adapter._resolve_message_context(room_id, sender, event_id, caption, source_content, relates_to)
    if ctx is None:
        return
    parsed = parse_gallery(source_content, max_media_bytes=adapter._max_media_bytes)
    urls, types, names, kinds, missing = await _fetch_gallery_items(adapter, event_id, parsed.items)
    event = await adapter._build_inbound_event(
        room_id, sender, event_id, caption, source_content, relates_to, ctx=ctx,
        message_type=gallery_message_type(kinds), media_urls=urls, media_types=types,
        media_filenames=names, media_msgtype=msgtype)
    note = missing_items_note(parsed.total, sorted(parsed.skipped + missing, key=lambda item: item.position),
                              parsed.over_limit)
    event.text = f"{event.text}\n\n{note}" if event.text and note else (event.text or note)
    logger.info("Matrix gallery %s: %d/%d items cached", event_id, len(urls), parsed.total)
    await adapter.handle_message(event)
