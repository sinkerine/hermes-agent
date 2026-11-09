"""MSC4274 galleries preserve one gated turn, item order and partial-failure context."""

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.event import MessageType
from plugins.platforms.matrix.adapter_gallery import (
    GallerySkip, gallery_message_type, missing_items_note, parse_gallery,
)
from tests.plugins.platforms.matrix.test_inbound_media_filename import make_adapter

PNG = b"\x89PNG\r\n\x1a\nimage-a"
JPEG = b"\xff\xd8\xffimage-b"
BOT = "@hermes:example.org"


def item(kind="m.image", name="a.png", url="mxc://example.org/a", **extra):
    return {"itemtype": kind, "body": name, "url": url, **extra}


def gallery(items=None, body="caption", **extra):
    return {"msgtype": "dm.filament.gallery", "body": body,
            "itemtypes": [item()] if items is None else items, **extra}


def event(content, event_id="$gallery", sender="@alice:example.org"):
    return SimpleNamespace(content=content, sender=sender, event_id=event_id,
                           room_id="!group:example.org", timestamp=int(time.time() * 1000))


def setup_adapter(monkeypatch, data=None, require_mention=False):
    adapter = make_adapter(monkeypatch)
    adapter._require_mention = require_mention
    adapter._client.download_media = AsyncMock(return_value=PNG) if data is None else AsyncMock(side_effect=data)
    adapter._get_display_name = AsyncMock(side_effect=lambda room, user: user.split(":")[0][1:])
    adapter._resolve_message_context = AsyncMock(wraps=adapter._resolve_message_context)
    return adapter


@pytest.mark.parametrize("value,reason", [
    (None, "invalid media item"),
    (item(itemtype="m.unknown"), "unsupported item type"),
    (item(itemtype=[]), "unsupported item type"),
    (item(url=""), "no media URL"),
    (item(url="https://example.org/a"), "not a Matrix media URL"),
    (item(url="", file={"url": "https://example.org/a"}), "not a Matrix media URL"),
    (item(info={"size": 101}), "larger than the 100-byte media limit"),
    (item(url="", file={"url": "mxc://example.org/a"}), "missing decryption keys"),
])
def test_parse_rejects_bad_items(value, reason):
    parsed = parse_gallery(gallery([value]), max_media_bytes=100)
    assert parsed.items == []
    assert parsed.skipped == [GallerySkip(1, "a.png" if isinstance(value, dict) else "", reason)]


@pytest.mark.parametrize("missing", ["k", "iv", "sha256"])
def test_parse_requires_encryption_keys(missing):
    file = {"url": "mxc://example.org/e", "key": {"k": "key"}, "iv": "iv", "hashes": {"sha256": "hash"}}
    if missing == "k":
        del file["key"]["k"]
    elif missing == "sha256":
        del file["hashes"]["sha256"]
    else:
        del file[missing]
    assert parse_gallery(gallery([item(url="", file=file)]), max_media_bytes=100).skipped[0].reason == (
        "missing decryption keys")


@pytest.mark.parametrize("items", [None, {}, "invalid"])
def test_missing_or_invalid_items(items):
    parsed = parse_gallery({"itemtypes": items}, max_media_bytes=100)
    assert parsed.total == 0
    assert missing_items_note(parsed.total, parsed.skipped, parsed.over_limit) == (
        "[Gallery: the message contained no media items]")


def test_parser_filename_size_and_limit():
    parsed = parse_gallery(gallery([item(filename="../unsafe\n.png", info={"size": "unknown"})] * 61),
                           max_media_bytes=100)
    assert parsed.total == 61 and parsed.over_limit == 1
    assert len(parsed.items) == 60 and not parsed.skipped
    assert parsed.items[0].filename == "unsafe.png"
    assert parsed.items[0].mimetype == "image/png"
    assert "1 more items beyond the 60-item gallery limit" in missing_items_note(61, [], 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("msgtype", ["dm.filament.gallery", "m.gallery"])
async def test_routes_aliases_and_deduplicates(monkeypatch, msgtype):
    adapter = setup_adapter(monkeypatch)
    incoming = event(gallery(msgtype=msgtype))
    await adapter._on_room_message(incoming)
    await adapter._on_room_message(incoming)
    adapter.handle_message.assert_awaited_once()
    adapter._client.download_media.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("content,sender", [
    (gallery(**{"m.relates_to": {"rel_type": "m.replace"}, "m.new_content": gallery()}), "@alice:example.org"),
    (gallery(msgtype="unknown"), "@alice:example.org"),
    (gallery(), BOT),
])
async def test_ignored_events_do_not_download(monkeypatch, content, sender):
    adapter = setup_adapter(monkeypatch)
    await adapter._on_room_message(event(content, sender=sender))
    adapter._client.download_media.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("body,extra,allowed,text", [
    ("no mention", {}, False, None),
    ("", {"m.mentions": {"user_ids": [BOT]}}, True, ""),
    ("look", {"format": "org.matrix.custom.html", "formatted_body":
              '<a href="https://matrix.to/#/@hermes:example.org">Hermes</a> look'}, True, "look"),
    (BOT + " look", {}, True, "look"),
])
async def test_gate_once_before_download(monkeypatch, body, extra, allowed, text):
    adapter = setup_adapter(monkeypatch, require_mention=True)
    await adapter._on_room_message(event(gallery(body=body, **extra)))
    adapter._resolve_message_context.assert_awaited_once()
    assert adapter._client.download_media.await_count == int(allowed)
    assert adapter.handle_message.await_count == int(allowed)
    if allowed:
        assert adapter.handle_message.await_args.args[0].text == text


@pytest.mark.asyncio
async def test_mixed_order_and_one_dispatch(monkeypatch):
    from gateway.platforms.base import get_video_cache_dir
    values = [PNG, b"video", b"pdf", JPEG, PNG]
    items = [item(info={"mimetype": "image/png"}),
             item("m.video", "clip.mp4", "mxc://example.org/v", info={"mimetype": "video/mp4"}),
             item("m.file", "report.pdf", "mxc://example.org/f", info={"mimetype": "application/pdf"}),
             item(name="b.jpg", url="mxc://example.org/b", info={"mimetype": "image/jpeg"}),
             item(name="c.png", url="mxc://example.org/c")]
    adapter = setup_adapter(monkeypatch, values)
    await adapter._on_room_message(event(gallery(items)))
    adapter.handle_message.assert_awaited_once()
    received = adapter.handle_message.await_args.args[0]
    assert [Path(path).read_bytes() for path in received.media_urls] == values
    assert received.media_types == ["image/png", "video/mp4", "application/pdf", "image/jpeg", "image/png"]
    assert received.media_filenames == ["a.png", "clip.mp4", "report.pdf", "b.jpg", "c.png"]
    assert Path(received.media_urls[1]).parent == get_video_cache_dir()
    assert received.message_type == MessageType.PHOTO
    assert [str(call.args[0]) for call in adapter._client.download_media.await_args_list] == [i["url"] for i in items]


@pytest.mark.asyncio
async def test_partial_failures_preserve_caption(monkeypatch):
    adapter = setup_adapter(monkeypatch, [PNG, None, RuntimeError("transport"), b"x" * 101, b"not image"])
    adapter._max_media_bytes = 100
    content = gallery([item(name=f"{i}.png", url=f"mxc://example.org/{i}") for i in range(1, 6)])
    await adapter._on_room_message(event(content))
    received = adapter.handle_message.await_args.args[0]
    assert len(received.media_urls) == 1
    assert received.text.startswith("caption\n\n[Gallery: 4 of 5 items could not be loaded:")
    for position in range(2, 6):
        assert f"item {position} '{position}.png'" in received.text
    assert "larger than the 100-byte media limit" in received.text
    assert "not a valid image" in received.text


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["caption", ""])
async def test_all_failed_still_dispatches(monkeypatch, body):
    adapter = setup_adapter(monkeypatch, [None, None])
    await adapter._on_room_message(event(gallery([item(), item(name="b.png")], body=body)))
    adapter.handle_message.assert_awaited_once()
    received = adapter.handle_message.await_args.args[0]
    assert received.message_type == MessageType.TEXT and not received.media_urls
    note = "[Gallery: none of the 2 items could be loaded: item 1 'a.png' (download failed), item 2 'b.png' (download failed)]"
    assert received.text == (body + "\n\n" if body else "") + note


@pytest.mark.asyncio
async def test_encryption_and_bad_hash(monkeypatch):
    crypto = pytest.importorskip("mautrix.crypto.attachments", reason="requires the matrix extra")
    ciphertext, metadata = crypto.encrypt_attachment(PNG)
    file = {**metadata.serialize(), "url": "mxc://example.org/encrypted"}
    bad = {**file, "hashes": {"sha256": "bad"}}
    adapter = setup_adapter(monkeypatch, [ciphertext, ciphertext, JPEG])
    await adapter._on_room_message(event(gallery([
        item(url="", file=file), item(name="bad.png", url="", file=bad),
        item(name="last.jpg", info={"mimetype": "image/jpeg"}),
    ])))
    received = adapter.handle_message.await_args.args[0]
    assert [Path(path).read_bytes() for path in received.media_urls] == [PNG, JPEG]
    assert "item 2 'bad.png'" in received.text


@pytest.mark.asyncio
@pytest.mark.parametrize("relation,body,expected", [
    ({"m.in_reply_to": {"event_id": "$reply"}}, "> <@bob:example.org> old text\n\nnew caption", "new caption"),
    ({"rel_type": "m.thread", "event_id": "$root"}, "", ""),
])
async def test_reply_and_thread_context(monkeypatch, relation, body, expected):
    adapter = setup_adapter(monkeypatch)
    content = gallery(body=body, **{"m.relates_to": relation})
    await adapter._on_room_message(event(content))
    received = adapter.handle_message.await_args.args[0]
    assert received.text == expected
    assert received.raw_message is content
    if "m.in_reply_to" in relation:
        assert received.reply_to_message_id == "$reply"
        assert received.reply_to_text == "old text"
        assert received.reply_to_author_id == "@bob:example.org"
    else:
        assert received.source.thread_id == "$root"


@pytest.mark.asyncio
@pytest.mark.parametrize("items,data,kind", [
    ([item("m.video", "v.mp4")], [b"video"], MessageType.VIDEO),
    ([item("m.file", "f.pdf")], [b"file"], MessageType.DOCUMENT),
    ([item("m.audio", "a.ogg")], [b"audio"], MessageType.DOCUMENT),
    ([item(), item("m.video", "v.mp4")], [None, b"video"], MessageType.VIDEO),
])
async def test_delivered_items_choose_type(monkeypatch, items, data, kind):
    adapter = setup_adapter(monkeypatch, data)
    await adapter._on_room_message(event(gallery(items)))
    assert adapter.handle_message.await_args.args[0].message_type == kind
    assert gallery_message_type([]) == MessageType.TEXT


@pytest.mark.parametrize("count,encrypted", [(2, False), (60, False), (2, True), (60, True)])
def test_sender_wire_shape(count, encrypted):
    crypto = pytest.importorskip("mautrix.crypto.attachments", reason="requires the matrix extra")
    _, metadata = crypto.encrypt_attachment(PNG)
    media = item(filename="sender.png", info={"mimetype": "image/png", "size": len(PNG),
        "w": 1, "h": 1, "duration": 0, "thumbnail_info": {"w": 1, "h": 1},
        "thumbnail_url": "mxc://example.org/thumb", "thumbnail_file": metadata.serialize()})
    if encrypted:
        media.pop("url")
        media["file"] = {**metadata.serialize(), "url": "mxc://example.org/e"}
    content = gallery([media] * count, format="org.matrix.custom.html", formatted_body="<b>caption</b>",
        **{"m.mentions": {"user_ids": [BOT]}, "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}})
    parsed = parse_gallery(content, max_media_bytes=100)
    assert len(parsed.items) == count and not parsed.skipped
    assert parsed.items[0].filename == "sender.png"
