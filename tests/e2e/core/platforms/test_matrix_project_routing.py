"""Project room lifecycle through the real gateway, local Matrix and recording model.

Build with --group dev --group test --extra matrix; no live accounts are used.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Event

import pytest

from tests.e2e.core.platforms._drv_matrix import MatrixDriver
from tests.e2e.core.platforms._helpers import Director, GatewayUnderTest
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
from tests.fakes.platforms._standin import wait_until

pytestmark = [pytest.mark.platforms("linux"), pytest.mark.spawns_gateway_lookalike]
pytest.importorskip("mautrix", reason="requires the matrix extra: build with --extra matrix")


class ProjectJourney:
    def __init__(self, driver: MatrixDriver, director: Director, gateway: GatewayUnderTest) -> None:
        self.driver = driver
        self.director = director
        self.gateway = gateway

    def diagnostics(self) -> str:
        return self.gateway.tail() + "\n" + self.driver.describe()

    def command(self, room: str, command: str, expected: str, *, guest: bool = False,
                thread: str | None = None) -> str:
        server = self.driver.standin
        before = {event["event_id"] for event in server.bot_messages(room, thread_root=thread)}
        sender = self.driver.other_user_id if guest else self.driver.user_id
        self.command_id = server.post(room, sender, command, thread_root=thread)
        replies = wait_until(
            lambda: [event for event in server.bot_messages(room, thread_root=thread)
                     if event["event_id"] not in before and expected in event["content"]["body"]],
            f"{command}: {expected}", on_timeout=self.diagnostics)
        return replies[-1]["content"]["body"]

    def post(self, room: str, token: str, *, thread: str | None = None) -> str:
        self.director.script(token, ToolCall("terminal", {"command": "pwd"}), Text(f"done-{token}"))
        return self.driver.standin.post(room, self.driver.user_id, f"[in:{token}] pwd?",
                                        thread_root=thread)

    def root(self, room: str, token: str) -> str:
        # A real timeline event is the thread root; its reply also closes the room turn.
        root = self.post(room, token)
        self.check(room, token, self.gateway.home, None)
        return root

    def check(self, room: str, token: str, cwd: Path, marker: str | None,
              *, thread: str | None = None) -> None:
        replies = wait_until(
            lambda: [event for event in self.driver.standin.bot_messages(room, thread_root=thread)
                     if f"done-{token}" in event["content"]["body"]],
            f"delivered {token}", timeout=120, on_timeout=self.diagnostics)
        assert len(replies) == 1
        assert replies[0]["sender"] == self.driver.bot_id
        relation = replies[0]["content"].get("m.relates_to", {})
        if thread:
            assert relation["rel_type"] == "m.thread"
            assert relation["event_id"] == thread
        else:
            assert relation.get("rel_type") != "m.thread"
        requests = self.director.requests[token]
        prompt = "\n".join(Director._text(message) for message in requests[0]["messages"]
                           if message["role"] == "system")
        for candidate in ("ALPHA-MARKER", "BETA-MARKER"):
            assert (candidate in prompt) == (candidate == marker), prompt
        assert str(cwd) in prompt
        tools = [message for message in requests[-1]["messages"] if message["role"] == "tool"]
        assert tools, requests
        result = json.loads(Director._text(tools[-1]))
        assert result["output"].strip() == str(cwd), result
        self.gateway.wait_idle()

    def session(self, token: str) -> tuple[str, str]:
        with sqlite3.connect(f"file:{self.gateway.db_path}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT DISTINCT s.id, s.cwd FROM sessions s JOIN messages m ON m.session_id=s.id "
                "WHERE m.role='user' AND m.content LIKE ?", (f"%[in:{token}]%",)).fetchall()
        assert len(rows) == 1, rows
        return rows[0]

    def cli(self, *args: str) -> None:
        result = self.gateway.run_cli("project", *args)
        assert result.returncode == 0, result.stdout + result.stderr


def test_matrix_project_routing_journey(tmp_path: Path) -> None:
    driver = MatrixDriver()
    director = Director()
    overlap_gate = Barrier(2, timeout=60)
    overlapped = Event()

    def respond(record):
        response = director(record)
        users = [message for message in record["body"]["messages"] if message["role"] == "user"]
        last_user = Director._text(users[-1]) if users else ""
        if isinstance(response, ToolCall) and any(f"[in:{token}]" in last_user for token in ("a1", "b1")):
            try:
                overlap_gate.wait()
            except BrokenBarrierError:
                return Text("concurrent project requests did not overlap")
            overlapped.set()
        return response

    workspace = tmp_path / "projects"
    workspace.mkdir()
    alpha = workspace / "alpha"
    beta = tmp_path / "existing" / "beta"
    beta.mkdir(parents=True)
    (beta / "AGENTS.md").write_text("BETA-MARKER\n", encoding="utf-8")
    driver.start()
    try:
        with FakeLLMServer(respond) as llm:
            config = driver.gateway_config()
            config["platforms"]["matrix"]["extra"]["group_user_allowed_commands"] = ["project"]
            config["gateway"] = {"project_routing": {"workspace_root": str(workspace)}}
            gateway = GatewayUnderTest(tmp_path, llm_base_url=llm.base_url, config=config,
                                       env=driver.gateway_env(), ready=driver.connected)
            journey = ProjectJourney(driver, director, gateway)
            journey.cli("create", "Beta", str(beta), "--slug", "beta")
            try:
                driver.arm_readiness()
                gateway.start(timeout=120)
                pid = gateway.proc.pid
                r1, r2 = driver.room_id, driver.other_room_id
                # These timeline turns predate the binding and provide actual thread roots.
                t1 = journey.root(r1, "p0")
                u1 = journey.root(r2, "u0")
                created = journey.command(r1, "!project create alpha", "Bound this room to alpha")
                assert str(alpha) in created
                assert (alpha / "sources").is_dir()
                agents = alpha / "AGENTS.md"
                assert agents.is_file()
                with agents.open("a", encoding="utf-8") as stream:
                    stream.write("\nALPHA-MARKER\n")
                assert str(beta) in journey.command(r2, "!project bind beta", "Bound this room to beta")
                journey.post(r1, "p1")
                journey.check(r1, "p1", gateway.home, None)
                journey.command(r1, "!new", "Session reset!")
                journey.post(r1, "p2")
                journey.check(r1, "p2", alpha, "ALPHA-MARKER")
                for mutation in ("create forbidden", "bind beta", "unbind"):
                    journey.command(r1, f"!project {mutation}", "Only an admin", guest=True)
                info = journey.command(r1, "!project info", "Room project:", guest=True)
                assert "alpha" in info and str(alpha) in info
                listing = journey.command(r1, "!project list", "Projects in this runtime profile", guest=True)
                assert "alpha" in listing and "beta" in listing and "*" in listing
                # Neither initial model response may proceed until both room turns are in flight.
                journey.post(r1, "a1", thread=t1)
                journey.post(r2, "b1", thread=u1)
                assert overlapped.wait(65), journey.diagnostics()
                assert not overlap_gate.broken
                journey.check(r1, "a1", alpha, "ALPHA-MARKER", thread=t1)
                journey.check(r2, "b1", beta, "BETA-MARKER", thread=u1)
                journey.command(r1, "!project info", "Room project:")
                t2 = journey.command_id
                journey.post(r1, "a2", thread=t2)
                journey.check(r1, "a2", alpha, "ALPHA-MARKER", thread=t2)
                a1_session = journey.session("a1")
                assert a1_session[0] != journey.session("a2")[0]
                assert "[in:a1]" not in json.dumps(director.requests["a2"])
                assert "[in:a2]" not in json.dumps(director.requests["a1"])
                refused = journey.command(r1, "!project bind beta", "Use --force")
                assert "alpha" in refused
                journey.command(r1, "!project bind beta --force", "Bound this room to beta")
                pin_info = journey.command(r1, "!project info", "This conversation keeps", thread=t1)
                assert str(alpha) in pin_info and str(beta) in pin_info
                journey.post(r1, "a3", thread=t1)
                journey.check(r1, "a3", alpha, "ALPHA-MARKER", thread=t1)
                journey.command(r1, "!project info", "Room project:")
                t3 = journey.command_id
                journey.post(r1, "a4", thread=t3)
                journey.check(r1, "a4", beta, "BETA-MARKER", thread=t3)
                assert gateway.alive() and gateway.proc.pid == pid
                gateway.stop()
                driver.arm_readiness()
                gateway.start(timeout=120)
                journey.post(r1, "a5", thread=t1)
                journey.check(r1, "a5", alpha, "ALPHA-MARKER", thread=t1)
                assert journey.session("a5") == (a1_session[0], str(alpha))
                journey.cli("archive", "beta")
                journey.command(r2, "!project info", "Room project:")
                u2 = journey.command_id
                before = list(llm.requests)
                archived = journey.command(r2, "[in:b2] hi", "archived", thread=u2)
                assert "beta" in archived
                assert "b2" not in director.requests
                assert llm.requests == before
                journey.command(r2, "!project unbind", "Room unbound")
                journey.command(r2, "!project info", "Room project: unbound")
                listing = journey.command(r2, "!project list", "Projects in this runtime profile")
                assert "alpha" in listing and "Beta (beta," not in listing
                journey.command(r2, "!project info", "Room project:")
                u3 = journey.command_id
                journey.post(r2, "b3", thread=u3)
                journey.check(r2, "b3", gateway.home, None, thread=u3)
                assert journey.session("a3")[0] == a1_session[0]
                assert journey.session("a4")[0] != a1_session[0]
                assert (alpha / "sources").is_dir() and (beta / "AGENTS.md").is_file()
                assert gateway.alive() and len(gateway.pids) == 2
            finally:
                overlap_gate.abort()
                gateway.stop()
    finally:
        driver.stop()
