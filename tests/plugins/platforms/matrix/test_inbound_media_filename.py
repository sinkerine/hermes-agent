"""Captioned single attachments retain the transport filename in their cache."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import time

import pytest


def make_adapter(monkeypatch):
    monkeypatch.setenv("MATRIX_REQUIRE_MENTION", "false")
    monkeypatch.setenv("MATRIX_AUTO_THREAD", "false")
    from gateway.config import PlatformConfig
    from plugins.platforms.matrix.adapter import MatrixAdapter
    adapter = MatrixAdapter(PlatformConfig(enabled=True, token="test", extra={
        "homeserver": "https://example.org", "user_id": "@hermes:example.org"}))
    adapter._startup_ts = time.time() - 10
    adapter.handle_message = AsyncMock()
    adapter._client = SimpleNamespace(download_media=AsyncMock(return_value=b"attachment"))
    adapter._resolve_room_identity = AsyncMock(return_value=SimpleNamespace(
        display_name="Group", room_topic=None, server_name="example.org", chat_type="group"))
    adapter._is_dm_room = AsyncMock(return_value=False)
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("msgtype,body,filename,suffix", [
    ("m.file", "see attached", "report.pdf", "report.pdf"),
    ("m.audio", "listen to this", "a.m4a", ".m4a"),
    ("m.file", "old.pdf", None, "old.pdf"),
])
async def test_single_media_cache_filename(monkeypatch, msgtype, body, filename, suffix):
    adapter = make_adapter(monkeypatch)
    content = {"msgtype": msgtype, "body": body, "url": "mxc://example.org/file"}
    if filename is not None:
        content["filename"] = filename
    await adapter._handle_media_message("!group:example.org", "@alice:example.org", "$file", 0,
                                        content, {}, msgtype)
    event = adapter.handle_message.await_args.args[0]
    assert Path(event.media_urls[0]).name.endswith(suffix)
    assert Path(event.media_urls[0]).read_bytes() == b"attachment"
    assert event.text == body
