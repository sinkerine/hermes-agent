"""Element gallery payloads survive mautrix deserialization and dispatch as one turn."""

import base64
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.event import MessageType
from tests.plugins.platforms.matrix.test_inbound_media_filename import make_adapter

matrix_types = pytest.importorskip("mautrix.types", reason="requires the matrix extra")
crypto = pytest.importorskip("mautrix.crypto.attachments", reason="requires the matrix extra")

BOT = "@hermes:example.org"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aQ9sAAAAASUVORK5CYII=")


# Transcribed from Element Web d698eb223e, apps/web/src/:
# utils/MediaCaptionContent.ts:110-125,177-196 (createGalleryEventContent/withMediaCaption),
# ContentMessages.ts:833-923 (item body=file.name, info, upload encryption),
# utils/messages.ts:46-131 (attachMentions/attachRelation), utils/Reply.ts:52-91
# (makeReplyMixIn/addReplyToMessageContent). No Element checkout is needed at runtime.
def element_gallery(count=2, *, caption="what images do you see?", encrypted=False,
                    mentions=(), relation=None, html=None, msgtype="dm.filament.gallery"):
    items, downloads, originals = [], {}, []
    for index in range(count):
        data = PNG + str(index).encode()
        url = f"mxc://example.org/item{index}"
        media = {"itemtype": "m.image", "body": f"image-{index}.png",
                 "info": {"mimetype": "image/png", "size": len(data), "w": 1, "h": 1}}
        if encrypted:
            ciphertext, metadata = crypto.encrypt_attachment(data)
            media["file"] = {**metadata.serialize(), "url": url}
            _, thumbnail = crypto.encrypt_attachment(PNG)
            media["info"].update({
                "thumbnail_info": {"mimetype": "image/png", "size": len(PNG), "w": 1, "h": 1},
                "thumbnail_file": {**thumbnail.serialize(), "url": f"mxc://example.org/thumb{index}"}})
            downloads[url] = ciphertext
        else:
            media["url"] = url
            downloads[url] = data
        items.append(media)
        originals.append(data)
    content = {"msgtype": msgtype, "body": caption, "itemtypes": items,
               "m.mentions": {"user_ids": list(mentions)} if mentions else {}}
    if relation is not None:
        content["m.relates_to"] = relation
    if html is not None:
        content.update(format="org.matrix.custom.html", formatted_body=html)
    return content, downloads, originals


def incoming(content, *, deserialize=True):
    raw = {"type": "m.room.message", "room_id": "!group:example.org", "sender": "@alice:example.org",
           "event_id": "$gallery", "origin_server_ts": int(time.time() * 1000), "content": content}
    # Syncer.handle_sync uses Event.deserialize; Megolm decryptions deserialize the same content.
    if deserialize:
        parsed = matrix_types.Event.deserialize(raw)
        assert parsed.content.serialize()["itemtypes"] == content["itemtypes"]
        return parsed
    return SimpleNamespace(content=content, sender=raw["sender"], event_id=raw["event_id"],
                           room_id=raw["room_id"], timestamp=raw["origin_server_ts"])


def sender_adapter(monkeypatch, downloads, *, dm=False, require_mention=False):
    adapter = make_adapter(monkeypatch)
    adapter._require_mention = require_mention
    adapter._is_dm_room = AsyncMock(return_value=dm)
    adapter._client.download_media = AsyncMock(side_effect=lambda url: downloads[str(url)])
    adapter._get_display_name = AsyncMock(return_value="Alice")
    adapter._resolve_message_context = AsyncMock(wraps=adapter._resolve_message_context)
    return adapter


def assert_delivery(adapter, content, originals, text):
    adapter.handle_message.assert_awaited_once()
    adapter._resolve_message_context.assert_awaited_once()
    received = adapter.handle_message.await_args.args[0]
    assert received.text == text
    assert received.media_types == ["image/png"] * len(originals)
    assert received.media_filenames == [media["body"] for media in content["itemtypes"]]
    assert all("filename" not in media for media in content["itemtypes"])
    assert [Path(path).read_bytes() for path in received.media_urls] == originals
    assert received.message_type == MessageType.PHOTO
    assert [str(call.args[0]) for call in adapter._client.download_media.await_args_list] == [
        media.get("url") or media["file"]["url"] for media in content["itemtypes"]]
    return received


@pytest.mark.asyncio
@pytest.mark.parametrize("deserialize", [False, True], ids=["dict", "mautrix"])
@pytest.mark.parametrize("msgtype", ["dm.filament.gallery", "m.gallery"])
async def test_live_two_png_shape(monkeypatch, deserialize, msgtype):
    content, downloads, originals = element_gallery(msgtype=msgtype)
    assert set(content) == {"msgtype", "body", "itemtypes", "m.mentions"}
    adapter = sender_adapter(monkeypatch, downloads)
    await adapter._on_room_message(incoming(content, deserialize=deserialize))
    assert_delivery(adapter, content, originals, "what images do you see?")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 60])
@pytest.mark.parametrize("deserialize", [False, True], ids=["dict", "mautrix"])
async def test_encrypted_sender_order_ignores_thumbnail_file(monkeypatch, count, deserialize):
    content, downloads, originals = element_gallery(count, encrypted=True)
    adapter = sender_adapter(monkeypatch, downloads)
    await adapter._on_room_message(incoming(content, deserialize=deserialize))
    assert_delivery(adapter, content, originals, content["body"])


@pytest.mark.asyncio
async def test_uncaptioned_dm(monkeypatch):
    content, downloads, originals = element_gallery(caption="")
    adapter = sender_adapter(monkeypatch, downloads, dm=True, require_mention=True)
    await adapter._on_room_message(incoming(content))
    received = assert_delivery(adapter, content, originals, "")
    assert received.source.chat_type == "dm"


@pytest.mark.asyncio
async def test_uncaptioned_group_requires_mention_before_download(monkeypatch):
    content, downloads, _ = element_gallery(caption="")
    adapter = sender_adapter(monkeypatch, downloads, require_mention=True)
    await adapter._on_room_message(incoming(content))
    adapter._resolve_message_context.assert_awaited_once()
    adapter._client.download_media.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncaptioned_bot_participated_thread_needs_no_mention(monkeypatch):
    relation = {"rel_type": "m.thread", "event_id": "$root", "is_falling_back": True,
                "m.in_reply_to": {"event_id": "$root"}}
    content, downloads, originals = element_gallery(caption="", relation=relation)
    adapter = sender_adapter(monkeypatch, downloads, require_mention=True)
    await adapter._threads.mark_async("$root")
    await adapter._on_room_message(incoming(content))
    received = assert_delivery(adapter, content, originals, "")
    assert received.source.thread_id == "$root"


@pytest.mark.asyncio
async def test_html_caption_bot_pill_and_intentional_mention(monkeypatch):
    content, downloads, originals = element_gallery(
        caption=f"{BOT} compare these", mentions=[BOT],
        html=f'<a href="https://matrix.to/#/{BOT}">{BOT}</a> compare these')
    adapter = sender_adapter(monkeypatch, downloads, require_mention=True)
    await adapter._on_room_message(incoming(content))
    assert_delivery(adapter, content, originals, "compare these")


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", [
    {"rel_type": "m.thread", "event_id": "$root", "is_falling_back": True,
     "m.in_reply_to": {"event_id": "$reply"}},
    {"m.in_reply_to": {"event_id": "$reply"}},
], ids=["thread", "reply"])
async def test_element_relation_mixins_without_reply_fallback(monkeypatch, relation):
    content, downloads, originals = element_gallery(relation=relation)
    adapter = sender_adapter(monkeypatch, downloads)
    await adapter._on_room_message(incoming(content))
    received = assert_delivery(adapter, content, originals, content["body"])
    assert received.reply_to_message_id == "$reply"
    assert received.source.thread_id == relation.get("event_id")
    assert not received.reply_to_text
