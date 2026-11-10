"""Restart reconciliation, stale removal, allowlist changes and the YAML disable toggle."""
from __future__ import annotations

import pytest

import hermes_yaml as yaml
from tests.e2e.core.platforms.test_matrix_command_descriptions import (
    ALPHA, BETA, DM, EVENT_TYPE, ROOMS, add_probe, await_descriptions,
    descriptions, manifest_for, puts, start,
)
from tests.waiting import wait_until

pytest_plugins = ["tests.e2e.core.platforms.test_matrix_command_descriptions"]
pytestmark = [pytest.mark.platforms("linux"), pytest.mark.spawns_gateway_lookalike]


def test_matrix_command_descriptions_restart_cleanup_and_disable(command_gateway) -> None:
    driver, gateway, llm = command_gateway
    probe = add_probe(gateway)
    driver.standin.set_power_levels(BETA, {driver.bot_id: 50})
    start(driver, gateway)
    expected = manifest_for(driver, gateway)
    probe_key = next(key for key, content in expected.items() if content["command"] == "mcd-probe")
    for room in (ALPHA, BETA, DM):
        await_descriptions(driver, gateway, room, expected)
    gateway.stop()

    checkpoint = driver.standin.checkpoint()
    start(driver, gateway)
    wait_until(lambda: {call.params["room_id"] for call in driver.standin.calls_after(checkpoint)
                        if call.method == "room_state"} == set(ROOMS),
               timeout=30, what="restart reads all joined room state")
    assert puts(driver, checkpoint) == []
    assert all(descriptions(driver, room) == expected for room in (ALPHA, BETA, DM))
    gateway.stop()

    probe.unlink()
    config_path = gateway.hermes_home / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["platforms"]["matrix"]["extra"]["allowed_rooms"] = [BETA]
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    updated = manifest_for(driver, gateway)
    assert probe_key not in updated
    checkpoint = driver.standin.checkpoint()
    start(driver, gateway)
    await_descriptions(driver, gateway, ALPHA, {})
    for room in (BETA, DM):
        await_descriptions(driver, gateway, room, updated)
    changes = puts(driver, checkpoint)
    assert all(not call.faulted and call.params["content"] == {} for call in changes)
    assert {call.params["state_key"] for call in changes if call.params["room_id"] == ALPHA} == set(expected)
    for room in (BETA, DM):
        assert [call.params["state_key"] for call in changes if call.params["room_id"] == room] == [probe_key]
    gateway.stop()

    config["matrix"] = {"command_descriptions": False}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    checkpoint = driver.standin.checkpoint()
    start(driver, gateway)
    for room in ROOMS:
        await_descriptions(driver, gateway, room, {})
    changes = puts(driver, checkpoint)
    assert all(not call.faulted and call.params["content"] == {} for call in changes)
    assert {call.params["room_id"] for call in changes} == {BETA, DM}
    for room in (BETA, DM):
        assert {call.params["state_key"] for call in changes if call.params["room_id"] == room} == set(updated)
    for room in ROOMS:
        assert all(event["content"] == {} for event in driver.standin.state_events(room, EVENT_TYPE)
                   if event["sender"] == driver.bot_id)
    assert llm.requests == []
