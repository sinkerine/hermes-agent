"""Project memory journey through a real gateway child and loopback services.

Requires the installed Matrix extra. All receipts and state stay in pytest's tmp dir.
"""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import shutil

import pytest

from tests.e2e.core.platforms._drv_matrix import MatrixDriver
from tests.e2e.core.platforms._helpers import Director, GatewayUnderTest
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
from tests.fakes.hindsight_standin import HindsightStandin
from tests.fakes.platforms._standin import wait_until

pytestmark = [pytest.mark.platforms("linux"), pytest.mark.spawns_gateway_lookalike]
pytest.importorskip("mautrix", reason="requires the matrix extra: build with --extra matrix")


class MemoryJourney:
    def __init__(self, driver, director, gateway, hindsight):
        self.driver, self.director = driver, director
        self.gateway, self.hindsight = gateway, hindsight
        self.receipts = []

    def diagnostics(self):
        return self.gateway.tail() + "\n" + self.driver.describe()

    def command(self, command, expected, *, guest=False):
        server, room = self.driver.standin, self.driver.room_id
        before = {event["event_id"] for event in server.bot_messages(room)}
        sender = self.driver.other_user_id if guest else self.driver.user_id
        event_id = server.post(room, sender, command)
        replies = wait_until(
            lambda: [event for event in server.bot_messages(room)
                     if event["event_id"] not in before and expected in event["content"]["body"]],
            command, timeout=120, on_timeout=self.diagnostics)
        actual = replies[-1]["content"]["body"]
        self.receipts.append({"command": command, "event_id": event_id, "guest": guest,
                              "expected": expected, "actual": replies[-1]})
        return event_id, actual

    def turn(self, token, *, thread=None, unbound=False, tool=False, body=None):
        room = self.driver.other_room_id if unbound else self.driver.room_id
        script_token = token if body is None else "none"
        body = f"[in:{token}] what do we know?" if body is None else body
        responses = [Text(f"done-{token}")]
        if tool:
            responses.insert(0, ToolCall("project_memory_recall", {"query": "kb", "bank": "team-kb"}))
        self.director.script(script_token, *responses)
        event_id = self.driver.standin.post(room, self.driver.user_id, body, thread_root=thread)
        replies = wait_until(
            lambda: [event for event in self.driver.standin.bot_messages(room, thread_root=thread)
                     if f"done-{token}" in event["content"]["body"]],
            f"delivered {token}", timeout=120, on_timeout=self.diagnostics)
        assert len(replies) == 1
        if thread:
            relation = replies[0]["content"]["m.relates_to"]
            assert relation["rel_type"] == "m.thread" and relation["event_id"] == thread
        self.gateway.wait_idle()
        request = self.director.requests[script_token][-1 if script_token == "none" else 0]
        self.receipts.append({"turn": token, "body": body, "event_id": event_id,
                              "expected": f"done-{token}", "actual": replies[0]})
        return request

    def syncs(self):
        path = self.gateway.hermes_home / "recorder.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()
                if json.loads(line)["hook"] == "sync_turn"] if path.exists() else []

    def requests(self, suffix):
        return [row for row in self.hindsight.requests() if row["method"] == "POST"
                and row["path"].endswith(suffix)]

    def bank_attachment_requests(self):
        return [row for row in self.hindsight.requests() if row["method"] == "PUT"
                or (row["method"] == "GET" and row["path"].endswith("/config"))]


def current_context(request):
    return Director._text([message for message in request["messages"] if message["role"] == "user"][-1])


def cached_prefix(request):
    return json.dumps({"system": [message for message in request["messages"] if message["role"] == "system"],
                       "tools": request["tools"]}, sort_keys=True)


def has_recall_tool(request):
    return any(tool["function"]["name"] == "project_memory_recall" for tool in request["tools"])


def test_matrix_project_memory_journey(tmp_path: Path) -> None:
    driver, director = MatrixDriver(), Director()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    driver.start()
    try:
        with HindsightStandin() as hindsight, FakeLLMServer(director) as llm:
            hindsight.seed("default-alpha", [{"text": "ALPHA_FACT"}])
            hindsight.seed("team-kb", [{"text": "TEAMKB_FACT"}, {"text": "ALPHA_FACT"}])
            config = driver.gateway_config()
            config["platforms"]["matrix"]["extra"]["group_user_allowed_commands"] = ["project"]
            config.update(memory={"provider": "recorder"},
                          project_memory={"hindsight_url": hindsight.url},
                          gateway={"project_routing": {"workspace_root": str(workspace)}})
            gateway = GatewayUnderTest(tmp_path, llm_base_url=llm.base_url, config=config,
                                       env=driver.gateway_env(), ready=driver.connected)
            shutil.copytree(Path(__file__).resolve().parents[3] / "fakes" / "memory_recorder_plugin",
                            gateway.hermes_home / "plugins" / "recorder")
            dotenv = gateway.hermes_home / ".env"
            with dotenv.open("a") as stream:
                stream.write("\nPROJECT_HINDSIGHT_API_KEY=test-key\n")
            dotenv.chmod(0o600)
            journey = MemoryJourney(driver, director, gateway, hindsight)
            restart_hindsight_request_index = None
            try:
                driver.arm_readiness()
                gateway.start(timeout=120)
                pid = gateway.proc.pid
                journey.command("!project create alpha", "Bound this room to alpha")
                thread, info = journey.command("!project memory", "No memory banks attached.")
                assert hindsight.url in info
                assert "retain-shared: on" in info and "retain-project: off" in info
                journey.command("!project memory init", "Attached bank default-alpha")
                creates = [row for row in hindsight.requests() if row["method"] == "PUT"]
                assert len(creates) == 1
                assert creates[0]["path"] == "/v1/default/banks/default-alpha"
                assert creates[0]["body"] == {"name": "alpha"}
                assert creates[0]["headers"]["Authorization"] == "Bearer test-key"
                journey.command("!project memory", "default-alpha (rw)")
                journey.command("!project memory add team-kb --recall-only", "Attached bank team-kb")
                _, info = journey.command("!project memory", "team-kb (recall-only)")
                assert info.index("default-alpha (rw)") < info.index("team-kb (recall-only)")
                journey.command("!project memory add x", "Only an admin", guest=True)
                _, info = journey.command("!project memory", "team-kb (recall-only)", guest=True)
                assert "x (" not in info
                first = journey.turn("combined", thread=thread)
                context = current_context(first)
                assert context.index("SHARED_FACT") < context.index("ALPHA_FACT") < context.index("TEAMKB_FACT")
                assert context.count("ALPHA_FACT") == 1
                assert has_recall_tool(first)
                wait_until(lambda: len(journey.syncs()) == 1, "shared default sync", on_timeout=journey.diagnostics)
                assert len(journey.requests("/memories/recall")) == 2
                assert journey.requests("/memories") == []
                before = len(journey.requests("/memories/recall"))
                journey.turn("tool", thread=thread, tool=True)
                tool_request = director.requests["tool"][-1]
                results = [json.loads(Director._text(message)) for message in tool_request["messages"]
                           if message["role"] == "tool"]
                assert results and {hit["bank"] for hit in results[-1]["results"]} == {"team-kb"}
                assert {hit["text"] for hit in results[-1]["results"]} == {"TEAMKB_FACT", "ALPHA_FACT"}
                recalls = journey.requests("/memories/recall")[before:]
                assert len(recalls) == 3
                assert recalls[-1]["path"].endswith("/team-kb/memories/recall")
                assert recalls[-1]["body"]["query"] == "kb"
                wait_until(lambda: len(journey.syncs()) == 2, "tool turn shared sync")
                journey.command("!project memory set retain-shared off", "settings updated")
                journey.command("!project memory set retain-project on", "settings updated")
                before = len(journey.requests("/memories/recall"))
                toggled = journey.turn("short", thread=thread, body="next")
                context = current_context(toggled)
                assert context.index("SHARED_FACT") < context.index("ALPHA_FACT") < context.index("TEAMKB_FACT")
                assert context.count("ALPHA_FACT") == 1
                recalls = journey.requests("/memories/recall")[before:]
                assert len(recalls) == 2
                assert {row["path"] for row in recalls} == {
                    "/v1/default/banks/default-alpha/memories/recall",
                    "/v1/default/banks/team-kb/memories/recall"}
                assert all(row["body"]["query"] == "[admin] next" for row in recalls)
                wait_until(lambda: len(journey.requests("/memories")) == 1, "project retain")
                assert len(journey.syncs()) == 2
                assert journey.requests("/memories")[0]["path"].endswith("/default-alpha/memories")
                assert cached_prefix(toggled) == cached_prefix(first)
                journey.command("!project memory remove team-kb", "Detached bank team-kb")
                removed = journey.turn("again", thread=thread)
                assert "TEAMKB_FACT" not in current_context(removed)
                assert cached_prefix(removed) == cached_prefix(first)
                wait_until(lambda: len(journey.requests("/memories")) == 2, "second project retain")
                assert gateway.alive() and gateway.proc.pid == pid
                hindsight.queue_fault("default-alpha", status=500)
                failed = journey.turn("still-there", thread=thread)
                assert "## Project memory" not in current_context(failed)
                assert "ALPHA_FACT" not in current_context(failed)
                assert gateway.alive()
                unbound = journey.turn("unbound", unbound=True)
                assert "## Project memory" not in current_context(unbound)
                assert not has_recall_tool(unbound)
                assert "SHARED_FACT" in current_context(unbound)
                wait_until(lambda: len(journey.syncs()) == 3, "unbound shared sync")
                gateway.stop()
                retained = len(journey.requests("/memories"))
                assert retained == 3
                assert len(journey.syncs()) == 3
                bank_requests_before_restart = journey.bank_attachment_requests()
                restart_hindsight_request_index = len(hindsight.requests())
                driver.arm_readiness()
                gateway.start(timeout=120)
                restarted = journey.turn("restarted", thread=thread)
                assert journey.bank_attachment_requests() == bank_requests_before_restart
                assert "ALPHA_FACT" in current_context(restarted)
                assert "TEAMKB_FACT" not in current_context(restarted)
                wait_until(lambda: len(journey.requests("/memories")) == retained + 1, "persisted project retain")
                assert len(journey.syncs()) == 3
                assert all(row["path"].endswith("/default-alpha/memories")
                           for row in journey.requests("/memories"))
                result = gateway.run_cli("project", "memory", "show", "alpha")
                journey.receipts.append({"command": "hermes project memory show alpha",
                                         "expected": "persisted bank and retain flags", "actual": result.stdout,
                                         "stderr": result.stderr, "returncode": result.returncode})
                assert result.returncode == 0, result.stdout + result.stderr
                assert "default-alpha (rw)" in result.stdout and "team-kb" not in result.stdout
                assert "retain-shared: off" in result.stdout and "retain-project: on" in result.stdout
                assert gateway.alive() and len(gateway.pids) == 2
                assert journey.bank_attachment_requests() == bank_requests_before_restart
            finally:
                gateway.stop()
                evidence = {"steps": journey.receipts, "model_requests": director.requests,
                            "hindsight_requests": hindsight.requests(), "gateway_pids": gateway.pids,
                            "restart_hindsight_request_index": restart_hindsight_request_index,
                            "matrix_calls": [asdict(call) for call in driver.standin.calls_after(0)],
                            "wording": "Mutation receipts followed by info; rw means read-write."}
                (tmp_path / "journey-receipts.json").write_text(json.dumps(evidence, indent=2))
    finally:
        driver.stop()
