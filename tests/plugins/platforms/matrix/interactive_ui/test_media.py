"""Upload preflight, magic detection and encrypted attachment integration."""

from copy import deepcopy
import asyncio
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.matrix.interactive_ui import INTERACTIVE_UI_KEY
from plugins.platforms.matrix.interactive_ui.media import prepare_uploads
from plugins.platforms.matrix.interactive_ui.protocol import InteractiveUiError
from .test_producer import present
from . import test_producer

producer = pytest.fixture(test_producer.producer.__wrapped__)


def image_ui(path):
    return {"state": {}, "root": "image", "nodes": [{"id": "image", "type": "image", "alt": "A local image", "upload": str(path)}]}


@pytest.mark.parametrize("magic,mimetype", [(b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                                          (b"GIF89a", "image/gif"), (b"RIFFxxxxWEBP", "image/webp")])
def test_magic_overrides_extension_and_model_mime(tmp_path, magic, mimetype):
    path = tmp_path / "image.untrusted"
    path.write_bytes(magic + b"data")
    ui = image_ui(path)
    ui["nodes"][0]["mimetype"] = "text/plain"
    uploads = prepare_uploads(ui, max_bytes=100)
    assert len(uploads) == 1 and uploads[0].mimetype == mimetype
    assert ui["nodes"][0]["mimetype"] == mimetype and "upload" not in ui["nodes"][0]


@pytest.mark.parametrize("kind", ["spoof", "directory", "missing", "relative", "oversize", "fifo"])
def test_invalid_local_images_are_refused(tmp_path, kind):
    path = tmp_path / "image.png"
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        import os
        os.mkfifo(path)
    elif kind != "missing":
        path.write_bytes(b"secret text" if kind == "spoof" else b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    value = "relative.png" if kind == "relative" else path
    result = prepare_uploads(image_ui(value), max_bytes=50)
    assert isinstance(result, InteractiveUiError)
    assert result.path == "nodes[0].upload" and result.code == "bad_value"
    assert str(path) not in result.hint and "secret text" not in result.hint


@pytest.mark.asyncio
@pytest.mark.parametrize("producer,encrypted", [
    ("off", False), ("optional", False), ("required", False),
    ("optional", True), ("required", True),
], indirect=["producer"])
async def test_upload_uses_room_policy_and_encrypted_file_form(producer, tmp_path, encrypted):
    path = tmp_path / "private-local-name.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    adapter = producer.adapter
    adapter._client.upload_media = AsyncMock(return_value="mxc://example.org/uploaded")
    from mautrix.types import RoomEncryptionStateEventContent
    adapter._client.state_store.encryption["!room:example.org"] = RoomEncryptionStateEventContent() if encrypted else None
    adapter._client.crypto = object() if encrypted or adapter._e2ee_mode == "required" else None
    result = await present(producer, image_ui(path))
    assert result["ok"]
    content = adapter._client.send_message_event.await_args.args[2]
    image = content[INTERACTIVE_UI_KEY]["nodes"][0]
    assert "upload" not in image
    kwargs = adapter._client.upload_media.await_args.kwargs
    assert kwargs == {"mime_type": "image/png", "filename": "image", "size": len(path.read_bytes())}
    if encrypted:
        from mautrix.crypto.attachments import decrypt_attachment
        file = image["file"]
        assert "url" not in image and file["v"] == "v2"
        assert decrypt_attachment(adapter._client.upload_media.await_args.args[0], file["key"]["k"],
                                  file["hashes"]["sha256"], file["iv"]) == path.read_bytes()
    else:
        assert image["url"] == "mxc://example.org/uploaded" and "file" not in image
    assert producer.store.get_by_event(result["event_id"]).payload == content[INTERACTIVE_UI_KEY]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["later_image", "bad_root", "budget", "url_conflict", "remote"])
async def test_full_preflight_before_any_upload(producer, tmp_path, invalid):
    path = tmp_path / "valid.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    ui = image_ui(path)
    if invalid == "bad_root":
        ui["root"] = "missing"
    if invalid == "later_image":
        ui = {"state": {}, "root": "r", "nodes": [{"id": "r", "type": "stack", "direction": "vertical", "children": ["image", "bad"]},
                                       ui["nodes"][0], {"id": "bad", "type": "image", "upload": str(tmp_path / "absent"), "alt": "bad"}]}
    if invalid == "budget":
        ui["ignored"] = "x" * 40000
    if invalid == "url_conflict":
        ui["nodes"][0]["url"] = "mxc://example.org/also"
    if invalid == "remote":
        ui["nodes"][0]["upload"] = "https://example.org/image.png"
    adapter = producer.adapter
    adapter._client.upload_media = AsyncMock()
    result = await present(producer, ui)
    assert not result["ok"] and "interactive_ui:" in result["error"]
    adapter._client.upload_media.assert_not_awaited()
    adapter._client.send_message_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_carousel_image_upload(producer, tmp_path):
    path = tmp_path / "image.jpg"
    path.write_bytes(b"\xff\xd8\xffimage")
    image = deepcopy(image_ui(path)["nodes"][0])
    image.pop("id")
    image.pop("type")
    ui = {"state": {}, "root": "r", "nodes": [{"id": "r", "type": "carousel", "label": "Images", "items": [{"id": "item", "title": "Image", "image": image}]}]}
    producer.adapter._client.upload_media = AsyncMock(return_value="mxc://example.org/image")
    assert (await present(producer, ui))["ok"]
    content = producer.adapter._client.send_message_event.await_args.args[2]
    assert content[INTERACTIVE_UI_KEY]["nodes"][0]["items"][0]["image"]["url"] == "mxc://example.org/image"


@pytest.mark.asyncio
@pytest.mark.parametrize("encrypted", [False, True])
async def test_upload_resolves_uncached_room_encryption(producer, tmp_path, encrypted):
    from mautrix.errors import MNotFound
    from mautrix.types import EventType, RoomEncryptionStateEventContent

    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    client = producer.adapter._client
    client.state_store.encryption.clear()
    client.get_state_event = AsyncMock(return_value=RoomEncryptionStateEventContent())
    if not encrypted:
        client.get_state_event.side_effect = MNotFound(404, "State event not found")
    client.upload_media = AsyncMock(return_value="mxc://example.org/image")
    result = await present(producer, image_ui(path))
    assert result["ok"]
    client.get_state_event.assert_awaited_once_with("!room:example.org", EventType.ROOM_ENCRYPTION)
    image = client.send_message_event.await_args.args[2][INTERACTIVE_UI_KEY]["nodes"][0]
    if encrypted:
        from mautrix.crypto.attachments import decrypt_attachment
        file = image["file"]
        assert decrypt_attachment(client.upload_media.await_args.args[0], file["key"]["k"],
                                  file["hashes"]["sha256"], file["iv"]) == path.read_bytes()
    else:
        assert image["url"] == "mxc://example.org/image" and "file" not in image


@pytest.mark.asyncio
async def test_upload_error_does_not_disclose_exception(producer, tmp_path, caplog):
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    producer.adapter._client.upload_media = AsyncMock(side_effect=ValueError("private credentials and generated code"))
    assert await present(producer, image_ui(path)) == {"ok": False, "error": "send_failed: image_upload"}
    assert not caplog.records
    producer.adapter._client.send_message_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_encrypted_metadata_budget_checked_before_upload(producer, tmp_path):
    from plugins.platforms.matrix.interactive_ui.content import build_ui_content
    from plugins.platforms.matrix.interactive_ui.fallback import render_fallback_markdown
    from plugins.platforms.matrix.interactive_ui.media import prepare_wire_uploads
    from plugins.platforms.matrix.adapter_html import sanitize_matrix_html

    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    from mautrix.types import RoomEncryptionStateEventContent
    producer.adapter._client.state_store.encryption["!room:example.org"] = RoomEncryptionStateEventContent()
    producer.adapter._client.upload_media = AsyncMock(return_value="mxc://example.org/image")
    ui = {"version": 1, "ui_id": "ui_budget", "revision": 0, "status": "active", "state": {}, "root": "r",
          "nodes": [{"id": "r", "type": "stack", "direction": "vertical", "children": ["text", "image"]},
                    {"id": "text", "type": "text", "text": ""}, image_ui(path)["nodes"][0]]}
    # Binary search the largest plain-image content, then prove encryption alone makes it overflow.
    low, high = 3000, 4000
    while low < high:
        middle = (low + high + 1) // 2
        ui["nodes"][1]["text"] = "&" * middle
        copy = deepcopy(ui)
        prepare_uploads(copy, max_bytes=100)
        plain = build_ui_content(copy, render_fallback_markdown(copy), sanitize_html=sanitize_matrix_html, thread_metadata=None)
        if isinstance(plain, InteractiveUiError):
            high = middle - 1
        else:
            low = middle
    ui["nodes"][1]["text"] = "&" * low
    copy = deepcopy(ui)
    uploads = prepare_uploads(copy, max_bytes=100)
    await prepare_wire_uploads(producer.adapter, "!room:example.org", uploads)
    encrypted = build_ui_content(copy, render_fallback_markdown(copy), sanitize_html=sanitize_matrix_html, thread_metadata=None)
    assert isinstance(encrypted, InteractiveUiError)
    result = await present(producer, ui, thread_metadata=None)
    assert "too_large" in result["error"]
    producer.adapter._client.upload_media.assert_not_awaited()
    producer.adapter._client.send_message_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("producer,unavailable", [
    (mode, unavailable) for mode in ("off", "optional", "required")
    for unavailable in ("lookup_error", "unknown", "missing_store", "encrypted_without_crypto")
] + [("required", "required_without_crypto"), ("optional", "missing_crypto"),
     ("off", "encrypted_support_disabled")], indirect=["producer"])
@pytest.mark.parametrize("replacement", [False, True])
async def test_upload_policy_refuses_before_side_effects(producer, tmp_path, caplog, unavailable, replacement):
    from mautrix.types import RoomEncryptionStateEventContent

    first = await present(producer) if replacement else None
    client = producer.adapter._client
    client.send_message_event.reset_mock()
    before = list(producer.store._db.iterdump())
    client.upload_media = AsyncMock(return_value="mxc://example.org/image")
    if unavailable == "lookup_error":
        client.state_store.is_encrypted = AsyncMock(side_effect=RuntimeError("private credentials and payload"))
    elif unavailable == "unknown":
        client.state_store.encryption.clear()
    elif unavailable == "missing_store":
        del client.state_store
    else:
        if unavailable == "missing_crypto":
            del client.crypto
        elif unavailable != "encrypted_support_disabled":
            client.crypto = None
        if unavailable != "required_without_crypto":
            client.state_store.encryption["!room:example.org"] = RoomEncryptionStateEventContent()
    path = tmp_path / "private-image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    ui = image_ui(path)
    original = deepcopy(ui)
    kwargs = {"replace_ui_id": first["ui_id"]} if first else {}
    result = await present(producer, ui, **kwargs)
    assert result == {"ok": False, "error": "send_failed: image_upload"}
    assert not caplog.records
    client.upload_media.assert_not_awaited()
    client.send_message_event.assert_not_awaited()
    assert list(producer.store._db.iterdump()) == before
    assert ui == original


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["policy", "upload"])
async def test_upload_cancellation_propagates(producer, tmp_path, stage):
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    client = producer.adapter._client
    client.upload_media = AsyncMock(return_value="mxc://example.org/image")
    before = list(producer.store._db.iterdump())
    if stage == "policy":
        client.state_store.is_encrypted = AsyncMock(side_effect=asyncio.CancelledError)
    else:
        client.upload_media.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await present(producer, image_ui(path))
    if stage == "policy":
        client.upload_media.assert_not_awaited()
    client.send_message_event.assert_not_awaited()
    assert list(producer.store._db.iterdump()) == before
    assert not producer.lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("uri_overflow", ["length", "content_budget"])
async def test_oversized_returned_mxc_refuses_card_without_store_change(producer, tmp_path, replacement, uri_overflow):
    first = await present(producer) if replacement else None
    client = producer.adapter._client
    client.send_message_event.reset_mock()
    uri = "mxc://example.org/" + "x" * (40000 if uri_overflow == "length" else 2000)
    client.upload_media = AsyncMock(return_value=uri)
    before = list(producer.store._db.iterdump())
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    ui = image_ui(path)
    if uri_overflow == "content_budget":
        # This URI is valid on its own but expands all three copies in the card content.
        ui = {"state": {}, "root": "r", "nodes": [
            {"id": "r", "type": "stack", "direction": "vertical", "children": ["text", "image"]},
            {"id": "text", "type": "text", "text": "&" * 3500}, ui["nodes"][0]]}
    kwargs = {"replace_ui_id": first["ui_id"]} if first else {}
    result = await present(producer, ui, **kwargs)
    error = "nodes[0].url: too_long:" if uri_overflow == "length" else "too_large:"
    assert not result["ok"] and error in result["error"]
    client.upload_media.assert_awaited_once()
    client.send_message_event.assert_not_awaited()
    assert list(producer.store._db.iterdump()) == before
