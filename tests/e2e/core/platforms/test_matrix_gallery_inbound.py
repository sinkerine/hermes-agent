"""Real gateway gallery journey against authenticated Matrix media and a recording model."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from tests.e2e.core.platforms._drv_matrix import MatrixDriver
from tests.e2e.core.platforms._helpers import Director, GatewayUnderTest
from tests.fakes.fake_llm_provider import FakeLLMServer, Text
from tests.fakes.platforms._standin import wait_until

pytestmark = [pytest.mark.platforms("linux"), pytest.mark.spawns_gateway_lookalike]
pytest.importorskip("mautrix", reason="requires the matrix extra")

# Tiny PNGs with distinct bytes, preserved through the native-image gateway path.
PNG_A = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aQ9sAAAAASUVORK5CYII=")
PNG_B = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def _image(url: str, name: str) -> dict:
    return {"itemtype": "m.image", "body": name, "filename": name, "url": url,
            "info": {"mimetype": "image/png"}}


def _last_user(request: dict) -> tuple[str, list[str]]:
    message = [message for message in request["messages"] if message["role"] == "user"][-1]
    content = message["content"]
    if isinstance(content, str):
        return content, []
    return ("\n".join(part["text"] for part in content if part["type"] == "text"),
            [part["image_url"]["url"] for part in content if part["type"] == "image_url"])


def _data_url(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode()


def test_matrix_gallery_one_turn_with_ordered_media_and_failures(tmp_path: Path) -> None:
    crypto = pytest.importorskip("mautrix.crypto.attachments", reason="requires the matrix extra")
    driver = MatrixDriver()
    director = Director()
    for token in ("gallery", "none", "after", "allfail"):
        director.script(token, Text(f"completed {token}"))
    driver.start()
    try:
        with FakeLLMServer(director) as llm:
            config = driver.gateway_config()
            config["agent"] = {"image_input_mode": "native"}
            config["model"] = {"supports_vision": True}
            env = {**driver.gateway_env(), "MATRIX_REQUIRE_MENTION": "true"}
            gateway = GatewayUnderTest(tmp_path, llm_base_url=llm.base_url, config=config,
                                       env=env, ready=driver.connected)
            try:
                driver.arm_readiness()
                gateway.start(timeout=120)
                server = driver.standin
                mentions = {"user_ids": [driver.bot_id]}

                def post(content: dict) -> str:
                    return server.post_content(driver.room_id, driver.user_id, content)

                def reply(token: str, root: str | None = None) -> list[dict]:
                    return wait_until(
                        lambda: [event for event in server.bot_messages(driver.room_id, thread_root=root)
                                 if f"completed {token}" in event["content"]["body"]],
                        f"gallery reply for {token}", timeout=120,
                        on_timeout=lambda: gateway.tail() + "\n" + driver.describe())

                a = server.put_media(PNG_A, media_id="a")
                v = server.put_media(b"video bytes", media_id="v")
                ciphertext, metadata = crypto.encrypt_attachment(PNG_B)
                b = server.put_media(ciphertext, media_id="b")
                encrypted = _image(b, "b.png")
                encrypted.pop("url")
                encrypted["file"] = {**metadata.serialize(), "url": b}
                root = post({"msgtype": "dm.filament.gallery", "body": "[in:gallery] compare these",
                    "m.mentions": mentions, "itemtypes": [
                        _image(a, "a.png"), {"itemtype": "m.video", "url": v, "body": "clip.mp4",
                            "filename": "clip.mp4", "info": {"mimetype": "video/mp4"}},
                        encrypted, _image("mxc://standin.local/c", "c.png")]})
                assert len(reply("gallery")) == 1
                assert director.turns["gallery"] == 1
                text, images = _last_user(director.requests["gallery"][-1])
                assert images == [_data_url(PNG_A), _data_url(PNG_B)]
                assert "[in:gallery] compare these" in text
                assert "clip.mp4" in text and "[Gallery: 1 of 4 items could not be loaded: item 4" in text
                assert [call.params["media_id"] for call in server.calls("media_download")] == ["a", "v", "b", "c"]

                b_plain = server.put_media(PNG_B)
                post({"msgtype": "m.gallery", "body": "", "m.mentions": mentions,
                      "m.relates_to": {"rel_type": "m.thread", "event_id": root},
                      "itemtypes": [_image(b_plain, "b.png"), _image(a, "a.png")]})
                threaded = reply("none", root)
                assert len(threaded) == 1
                assert threaded[0]["content"]["m.relates_to"]["rel_type"] == "m.thread"
                assert threaded[0]["content"]["m.relates_to"]["event_id"] == root
                assert director.turns["none"] == 1
                text, images = _last_user(director.requests["none"][-1])
                assert images == [_data_url(PNG_B), _data_url(PNG_A)]
                assert "[Gallery:" not in text and "compare these" not in text

                d = server.put_media(PNG_A, media_id="d")
                e = server.put_media(PNG_B, media_id="e")
                post({"msgtype": "dm.filament.gallery", "body": "[in:gated] hidden",
                      "itemtypes": [_image(d, "d.png"), _image(e, "e.png")]})
                post({"msgtype": "dm.filament.gallery", "body": "[in:edited] hidden",
                      "m.mentions": mentions, "m.new_content": {"msgtype": "m.gallery", "body": "[in:edited]"},
                      "m.relates_to": {"rel_type": "m.replace", "event_id": root},
                      "itemtypes": [_image(d, "d.png")]})
                post({"msgtype": "m.text", "body": "[in:after] done?", "m.mentions": mentions})
                assert len(reply("after")) == 1
                assert "gated" not in director.turns and "edited" not in director.turns
                assert not [call for call in server.calls("media_download") if call.params["media_id"] in {"d", "e"}]

                post({"msgtype": "dm.filament.gallery", "body": "[in:allfail] unavailable",
                      "m.mentions": mentions, "itemtypes": [
                          _image("mxc://standin.local/missing1", "missing1.png"),
                          _image("mxc://standin.local/missing2", "missing2.png")]})
                assert len(reply("allfail")) == 1
                assert director.turns["allfail"] == 1
                text, images = _last_user(director.requests["allfail"][-1])
                assert not images
                assert "[in:allfail] unavailable\n\n[Gallery: none of the 2 items could be loaded:" in text
                assert not [call for call in server.calls if call.faulted and isinstance(call.response, dict)
                            and call.response.get("errcode") == "M_UNRECOGNIZED"]
                assert server.bot_messages(driver.other_room_id) == []
            finally:
                gateway.stop()
    finally:
        driver.stop()
