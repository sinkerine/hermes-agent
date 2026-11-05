"""The real Matrix adapter connects and replies through a local homeserver and model.

Build the disposable interpreter with --group dev --group test --extra matrix.
No Matrix account, provider credential, or developer HOME is used.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import requests

from tests.e2e.core.platforms._drv_matrix import MatrixDriver
from tests.e2e.core.platforms._helpers import Director, GatewayUnderTest
from tests.fakes.fake_llm_provider import FakeLLMServer, Text
from tests.fakes.platforms._standin import wait_until
from tests.fakes.platforms.matrix_standin import BOT_USER_ID, MatrixStandin

pytestmark = [pytest.mark.platforms("linux"), pytest.mark.spawns_gateway_lookalike]

pytest.importorskip("mautrix", reason="requires the matrix extra: build the test env with --extra matrix")


def test_matrix_adapter_replies_in_room_and_thread(tmp_path: Path) -> None:
    driver = MatrixDriver()
    director = Director()
    director.script("room", Text("Matrix room reply"))
    director.script("thread", Text("Matrix thread reply"))
    director.script("continuation", Text("Matrix restarted thread continuation"))
    driver.start()
    try:
        with FakeLLMServer(director) as llm:
            gateway = GatewayUnderTest(tmp_path, llm_base_url=llm.base_url,
                                       config=driver.gateway_config(), env=driver.gateway_env(),
                                       ready=driver.connected)
            try:
                driver.arm_readiness()
                assert not driver.connected()
                gateway.start(timeout=120)
                assert gateway.alive()
                root = driver.standin.post(driver.room_id, driver.user_id, "[in:room] hello")

                def diagnostics() -> str:
                    return gateway.tail() + "\n" + driver.describe()

                room_replies = wait_until(
                    lambda: [event for event in driver.standin.bot_messages(driver.room_id)
                             if "Matrix room reply" in event["content"]["body"]],
                    "scripted Matrix room reply", on_timeout=diagnostics)
                assert "m.thread" != room_replies[0]["content"].get("m.relates_to", {}).get("rel_type")
                assert director.requests["room"]
                driver.standin.post(driver.room_id, driver.user_id, "[in:thread] hello", thread_root=root)
                thread_replies = wait_until(
                    lambda: [event for event in driver.standin.bot_messages(driver.room_id, thread_root=root)
                             if "Matrix thread reply" in event["content"]["body"]],
                    "scripted Matrix thread reply", on_timeout=diagnostics)
                relation = thread_replies[0]["content"]["m.relates_to"]
                assert relation["rel_type"] == "m.thread"
                assert relation["event_id"] == root
                assert director.requests["thread"]
                gateway.stop()
                driver.arm_readiness()
                assert not gateway.alive()
                assert not driver.connected(), "old calls must not satisfy restart readiness"
                gateway.start(timeout=120)
                assert gateway.alive()
                assert driver.connected()
                driver.standin.post(driver.room_id, driver.user_id, "[in:continuation] continue",
                                    thread_root=root)
                continued = wait_until(
                    lambda: [event for event in driver.standin.bot_messages(driver.room_id, thread_root=root)
                             if "Matrix restarted thread continuation" in event["content"]["body"]],
                    "fresh Matrix thread reply after restart", timeout=120, on_timeout=diagnostics)
                assert len(continued) == 1
                assert continued[0]["content"]["m.relates_to"]["rel_type"] == "m.thread"
                assert continued[0]["content"]["m.relates_to"]["event_id"] == root
                assert director.requests["continuation"]
                assert driver.standin.bot_messages(driver.other_room_id) == []
                assert not [call for call in driver.standin.calls
                            if call.faulted and call.response.get("errcode") == "M_UNRECOGNIZED"], diagnostics()
            finally:
                gateway.stop()
    finally:
        driver.stop()


def test_matrix_readiness_excludes_inflight_sync_and_failed_calls() -> None:
    """A poll entered before arming cannot count when its response is recorded later."""
    entered = threading.Event()
    release = asyncio.Event()

    class PausedSyncStandin(MatrixStandin):
        async def _api_sync(self, params: dict[str, Any]) -> dict[str, Any]:
            if params.get("pause") == "true":
                entered.set()
                await release.wait()
            return await super()._api_sync(params)

    driver = MatrixDriver()
    driver.standin = PausedSyncStandin()
    with driver.standin as server, ThreadPoolExecutor(max_workers=1) as pool:
        base = server.base_url + "/_matrix/client/v3"
        headers = {"Authorization": f"Bearer {server.token}"}
        assert not driver.connected()
        assert requests.get(base + "/account/whoami", headers=headers, timeout=5).status_code == 200
        old_poll = pool.submit(requests.get, base + "/sync", headers=headers,
                               params={"since": "0", "pause": "true"}, timeout=10)
        try:
            assert entered.wait(5), "old sync did not enter the stand-in"
            driver.arm_readiness()
            assert not driver.connected()
            assert requests.get(base + "/account/whoami", headers=headers, timeout=5).status_code == 200
        finally:
            async def resume() -> None:
                release.set()
            server.run_in_loop(resume())
        assert old_poll.result(timeout=5).status_code == 200
        assert server.calls("sync"), "old poll must remain in diagnostic history"
        assert not driver.connected(), "late completion of an old poll must not count"
        server.fail("sync", {"errcode": "M_UNKNOWN", "error": "scripted"}, status=500)
        assert requests.get(base + "/sync", headers=headers, params={"since": "0"},
                            timeout=5).status_code == 500
        assert not driver.connected()
        assert requests.get(base + "/sync", headers=headers, params={"since": "0"},
                            timeout=5).status_code == 200
        assert driver.connected()
        driver.arm_readiness()
        server.fail("whoami", {"errcode": "M_UNKNOWN_TOKEN", "error": "scripted"}, status=401)
        assert requests.get(base + "/account/whoami", headers=headers, timeout=5).status_code == 401
        assert requests.get(base + "/sync", headers=headers, params={"since": "0"},
                            timeout=5).status_code == 200
        assert not driver.connected()


def test_matrix_standin_sync_faults_and_visible_edits() -> None:
    """Keep the fake honest about cursors, retries, failures, and edited thread text."""
    room_id = "!contract:standin.local"
    user_id = "@admin:standin.local"
    with MatrixStandin() as server, requests.Session() as client:
        server.add_room(room_id, "Contract", [BOT_USER_ID, user_id])
        client.headers["Authorization"] = f"Bearer {server.token}"
        base = server.base_url + "/_matrix/client/v3"
        initial = client.get(base + "/sync", timeout=5).json()
        states = initial["rooms"]["join"][room_id]["state"]["events"]
        assert any(event["type"] == "m.room.name" and event["content"]["name"] == "Contract"
                   for event in states)
        assert {event["state_key"] for event in states if event["type"] == "m.room.member"} == {
            BOT_USER_ID, user_id}
        root = server.post(room_id, user_id, "root")
        params = {"since": initial["next_batch"], "timeout": "2000"}
        delta = client.get(base + "/sync", params=params, timeout=5).json()
        assert [event["event_id"] for event in delta["rooms"]["join"][room_id]["timeline"]["events"]] == [root]
        assert client.get(base + "/sync", params=params, timeout=5).json() == delta
        empty = client.get(base + "/sync", params={"since": delta["next_batch"], "timeout": "20"},
                           timeout=5).json()
        assert empty["rooms"]["join"] == {}
        assert empty["next_batch"] == delta["next_batch"]

        send_url = base + f"/rooms/{room_id}/send/m.room.message"
        content = {"msgtype": "m.text", "body": "draft", "m.relates_to": {
            "rel_type": "m.thread", "event_id": root}}
        fault = server.fail("send", {"errcode": "M_FORBIDDEN", "error": "scripted"}, status=403)
        assert client.put(send_url + "/first", json=content, timeout=5).status_code == 403
        assert fault.fired == 1
        sent = client.put(send_url + "/first", json=content, timeout=5).json()
        assert client.put(send_url + "/first", json=content, timeout=5).json() == sent
        edit = {"msgtype": "m.text", "body": "* final", "m.new_content": {"msgtype": "m.text", "body": "final"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": sent["event_id"]}}
        assert client.put(send_url + "/edit", json=edit, timeout=5).status_code == 200
        visible = server.bot_messages(room_id, thread_root=root)
        assert len(visible) == 1
        assert visible[0]["content"] == {**content, "body": "final"}
        assert server.bot_messages(room_id) == []
        unknown = client.get(base + "/unimplemented", timeout=5)
        assert unknown.status_code == 404
        assert unknown.json()["errcode"] == "M_UNRECOGNIZED"
        assert server.calls("GET /_matrix/client/v3/unimplemented")[0].faulted
        assert server.calls("send")[0].faulted
