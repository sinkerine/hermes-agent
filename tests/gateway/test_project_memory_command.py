"""Project memory commands against a real runtime profile DB and HTTP service."""
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_identity import RoutingIdentity
from hermes_cli import projects_db as db
from hermes_cli.config import atomic_config_write
from tests.fakes.hindsight_standin import HindsightStandin


@pytest.fixture
def room(tmp_path, monkeypatch):
    homes = {name: tmp_path / name for name in ("launch", "receiving", "runtime")}
    for home in homes.values():
        home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(homes["launch"]))
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._primary_profile_name = "default"
    runner._profile_configs = {"receiving": GatewayConfig(platforms={
        Platform.MATRIX: PlatformConfig(extra={"group_allow_admin_from": ["admin"],
                                              "group_user_allowed_commands": ["project"]})})}
    runner.session_store = SimpleNamespace(get_session_metadata=lambda *args: None)
    with db.connect_closing(db_path=homes["runtime"] / "projects.db") as conn:
        pid = db.create_project(conn, name="Alpha Team", slug="alpha", primary_path=str(homes["runtime"]))
        db.set_chat_binding(conn, platform="matrix", transport_profile="receiving", chat_id="!room:test",
                            project_id=pid, bound_by="admin")
    return runner, homes, pid


async def call(room, args="", *, user="admin", chat="!room:test"):
    runner, homes, _ = room
    source = SessionSource(platform=Platform.MATRIX, chat_id=chat, user_id=user,
                           chat_type="group", profile="runtime")
    source._identity = RoutingIdentity(transport_profile="receiving", runtime_profile="runtime",
                                       authorization_home=homes["receiving"], runtime_home=homes["runtime"])
    return await runner._handle_project_command(MessageEvent(text="/project memory " + args, source=source))


def banks(room):
    with db.connect_closing(db_path=room[1]["runtime"] / "projects.db") as conn:
        return db.list_memory_banks(conn, room[2])


@pytest.mark.asyncio
async def test_live_management_and_runtime_profile(room):
    with HindsightStandin() as server:
        atomic_config_write(room[1]["runtime"] / "config.yaml", {"project_memory": {"hindsight_url": server.url}})
        assert "runtime-alpha" in await call(room, "init")
        request = server.requests()[0]
        assert request["method"] == "PUT" and request["path"] == "/v1/default/banks/runtime-alpha"
        assert request["body"] == {"name": "Alpha Team"}
        assert "Warning" in await call(room, "add kb --recall-only")
        assert [(b.bank_id, b.writable) for b in banks(room)] == [("runtime-alpha", True), ("kb", False)]
        assert "updated" in await call(room, "set retain-shared off")
        assert "updated" in await call(room, "set retain-project on")
        info = await call(room)
        assert server.url in info and "retain-shared: off" in info and "retain-project: on" in info
        assert info.index("runtime-alpha (rw)") < info.index("kb (recall-only)")
        assert "No service data was deleted" in await call(room, "remove runtime-alpha")
        assert "runtime-alpha" in server.banks
        assert [b.bank_id for b in banks(room)] == ["kb"]
        assert "not attached" in await call(room, "remove absent")
        assert not (room[1]["launch"] / "projects.db").exists()
        assert not (room[1]["receiving"] / "projects.db").exists()


@pytest.mark.asyncio
async def test_admin_gate_info_invalid_ids_and_unbound(room):
    for args in ("init", "add kb", "remove kb", "set retain-shared off"):
        assert "Only an admin" in await call(room, args, user="guest")
    assert "not configured" in await call(room, user="guest")
    assert "no project binding" in await call(room, "init", chat="!unbound:test")
    for args in ("init ../bad", "add ../bad", "remove ../bad"):
        assert "Invalid bank id" in await call(room, args)
    assert banks(room) == []


@pytest.mark.asyncio
async def test_init_service_failure_and_db_retry(room, monkeypatch):
    assert "not configured" in await call(room, "init")
    assert banks(room) == []
    with HindsightStandin() as server:
        atomic_config_write(room[1]["runtime"] / "config.yaml", {"project_memory": {"hindsight_url": server.url}})
        server.queue_fault("runtime-alpha", status=500)
        assert "request failed" in await call(room, "init")
        assert banks(room) == []
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise sqlite3.OperationalError("database unavailable")
            patch.setattr(db, "add_memory_bank", fail)
            assert "database unavailable" in await call(room, "init")
        assert banks(room) == [] and "runtime-alpha" in server.banks
        assert "Attached" in await call(room, "init")
        assert "Attached" in await call(room, "init")
        assert len(banks(room)) == 1
        assert len([r for r in server.requests() if r["method"] == "PUT"]) == 4


@pytest.mark.asyncio
async def test_offline_attach_invalid_config_dangling_and_usage(room):
    assert "Attached" in await call(room, "add offline --recall-only")
    for args in ("wat", "init a b", "add", "add a --bad", "remove", "set x on", "set retain-shared maybe"):
        assert (await call(room, args)).startswith("/project [")
    atomic_config_write(room[1]["runtime"] / "config.yaml", {"project_memory": {"hindsight_url": "file:///bad"}})
    assert "Invalid project memory service" in await call(room)
    assert "Invalid project memory service" in await call(room, "init")
    assert len(banks(room)) == 1
    with db.connect_closing(db_path=room[1]["runtime"] / "projects.db") as conn:
        db.delete_project(conn, room[2])
    assert "missing project" in await call(room)


def test_matrix_bang_mapping():
    from plugins.platforms.matrix.adapter import _normalize_matrix_bang_command
    assert _normalize_matrix_bang_command("!project memory add kb") == "/project memory add kb"
