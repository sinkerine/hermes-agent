"""MSC4391 publication and dispatch through a real gateway and loopback Matrix API."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from itertools import pairwise
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from plugins.platforms.matrix.adapter_commands import (
    COMMAND_DESCRIPTION_EVENT_TYPE as EVENT_TYPE,
    is_valid_description,
)
from tests.e2e.core.platforms._drv_matrix import MatrixDriver
from tests.e2e.core.platforms._helpers import Director, GatewayUnderTest, hermetic_env
from tests.fakes.fake_llm_provider import FakeLLMServer
from tests.waiting import wait_until

pytestmark = [pytest.mark.platforms("linux"), pytest.mark.spawns_gateway_lookalike]

ALPHA = "!alpha:standin.local"
BETA = "!beta:standin.local"
GAMMA = "!gamma:standin.local"
DM = "!dm:standin.local"
ROOMS = (ALPHA, BETA, GAMMA, DM)
INTRUDER = "@intruder:standin.local"


@pytest.fixture
def command_gateway(tmp_path: Path):
    # Missing encryption dependencies must fail this feature gate rather than skip it.
    import mautrix.crypto

    driver = MatrixDriver()
    members = [driver.bot_id, driver.user_id, INTRUDER]
    for room in ROOMS:
        driver.standin.add_room(room, room, members if room != DM else members[:2])
    driver.standin.set_power_levels(BETA, {driver.bot_id: 0})
    driver.start()
    try:
        with FakeLLMServer(Director()) as llm:
            config = driver.gateway_config()
            config["platforms"]["matrix"]["extra"]["allowed_rooms"] = [ALPHA, BETA]
            env = driver.gateway_env()
            env["MATRIX_ALLOWED_USERS"] = driver.user_id
            gateway = GatewayUnderTest(tmp_path, llm_base_url=llm.base_url, config=config,
                                       env=env, ready=driver.connected)
            try:
                yield driver, gateway, llm
            finally:
                gateway.stop()
    finally:
        driver.stop()


def manifest_for(driver: MatrixDriver, gateway: GatewayUnderTest) -> dict:
    """Derive the expected dispatch surface in the same disposable profile as the child."""
    script = ("import json; from hermes_cli.plugins import discover_plugins; discover_plugins(); "
              "from plugins.platforms.matrix.adapter_commands import build_command_manifest; "
              f"print(json.dumps(build_command_manifest({driver.bot_id!r})))")
    result = subprocess.run([sys.executable, "-c", script], cwd=gateway.home,
                            env=hermetic_env(gateway.home, driver.gateway_env()),
                            text=True, capture_output=True, timeout=30, check=True)
    return json.loads(result.stdout)


def descriptions(driver: MatrixDriver, room: str) -> dict:
    return {event["state_key"]: event["content"]
            for event in driver.standin.state_events(room, EVENT_TYPE)
            if event["sender"] == driver.bot_id and event["content"]}


def puts(driver: MatrixDriver, checkpoint: int = 0) -> list:
    return [call for call in driver.standin.calls_after(checkpoint)
            if call.method == "put_state" and call.params["event_type"] == EVENT_TYPE]


def await_descriptions(driver: MatrixDriver, gateway: GatewayUnderTest, room: str,
                       expected: dict) -> None:
    try:
        wait_until(lambda: descriptions(driver, room) == expected, timeout=100,
                   what=f"complete command state in {room}")
    except AssertionError as exc:
        actual = descriptions(driver, room)
        missing = {key: value for key, value in expected.items() if actual.get(key) != value}
        extra = {key: value for key, value in actual.items() if key not in expected}
        raise AssertionError(f"{exc}\nMissing/different: {missing}\nExtra: {extra}"
                             f"\n{gateway.tail()}\n{driver.describe()}") from exc


def start(driver: MatrixDriver, gateway: GatewayUnderTest) -> None:
    driver.arm_readiness()
    assert not driver.connected()
    gateway.start(timeout=120)
    assert gateway.alive()


def add_probe(gateway: GatewayUnderTest) -> Path:
    skill = gateway.hermes_home / "skills" / "mcd-probe" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("---\nname: mcd-probe\ndescription: Command publication probe\n---\n"
                     "Reply with the probe result.\n", encoding="utf-8")
    return skill


def test_matrix_command_descriptions_publication_execution_reload_and_power(command_gateway, monkeypatch) -> None:
    driver, gateway, llm = command_gateway
    fault = driver.standin.fail("put_state", {"errcode": "M_LIMIT_EXCEEDED", "error": "slow",
                                             "retry_after_ms": 1}, status=429)
    start(driver, gateway)
    # Startup seeds bundled skills into a fresh HOME before connecting the adapter.
    expected = manifest_for(driver, gateway)
    assert expected
    for room in (ALPHA, DM):
        await_descriptions(driver, gateway, room, expected)
        for key, content in descriptions(driver, room).items():
            digest = hashlib.sha256((content["command"] + driver.bot_id).encode()).digest()
            assert key == base64.standard_b64encode(digest).decode()
            assert is_valid_description(content)
        status = next(c for c in expected.values() if c["command"] == "status")
        assert status["parameters"] == []  # The registry has no status args_hint.
    calls = puts(driver)
    assert {call.params["room_id"] for call in calls} == {ALPHA, DM}
    assert all(b.at - a.at >= 0.19 for a, b in pairwise(calls))
    assert fault.fired == 1
    failed = next(call for call in calls if call.faulted)
    index = calls.index(failed)
    retried = calls[index + 1]
    assert not retried.faulted
    assert retried.params == failed.params
    assert retried.at - failed.at >= 1

    content = {"msgtype": "m.text", "body": "/status",
               "m.mentions": {"user_ids": [driver.bot_id]},
               "org.matrix.msc4391.command": {"command": "status", "arguments": {}}}
    driver.standin.post_content(ALPHA, driver.user_id, content)
    wait_until(lambda: any("Hermes Gateway Status" in e["content"]["body"]
                           for e in driver.standin.bot_messages(ALPHA)),
               timeout=30, what="gateway status reply")
    assert llm.requests == []
    before = driver.standin.bot_messages(ALPHA)
    driver.standin.post_content(ALPHA, INTRUDER, content)
    # A bounded observation window is the access contract, not a publication delay.
    assert not threading.Event().wait(10)
    assert driver.standin.bot_messages(ALPHA) == before
    assert llm.requests == []

    add_probe(gateway)
    updated = manifest_for(driver, gateway)
    assert any(c["command"] == "mcd-probe" for c in updated.values())
    checkpoint = driver.standin.checkpoint()
    entered = threading.Event()
    release = asyncio.Event()
    original_put = driver.standin._api_put_state

    async def paused_put(params):
        entered.set()
        await release.wait()
        return await original_put(params)

    monkeypatch.setattr(driver.standin, "_api_put_state", paused_put)
    try:
        driver.standin.post(ALPHA, driver.user_id, "/reload-skills")
        assert entered.wait(10), "reload did not start publication"
        wait_until(lambda: len(driver.standin.bot_messages(ALPHA)) > len(before), timeout=10,
                   what="reload reply while publication is blocked")
        assert all(descriptions(driver, room) == expected for room in (ALPHA, DM))
    finally:
        async def resume() -> None:
            release.set()
        driver.standin.run_in_loop(resume())
    for room in (ALPHA, DM):
        await_descriptions(driver, gateway, room, updated)
    assert {call.params["room_id"] for call in puts(driver, checkpoint)} == {ALPHA, DM}
    driver.standin.set_power_levels(BETA, {driver.bot_id: 50})
    await_descriptions(driver, gateway, BETA, updated)
    assert descriptions(driver, GAMMA) == {}
    assert not [call for call in puts(driver) if call.params["room_id"] == GAMMA]
    assert llm.requests == []
    assert not [call for call in driver.standin.calls
                if call.faulted and call.response.get("errcode") == "M_UNRECOGNIZED"]
