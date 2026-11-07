"""Sandbox-only shared provider with durable lifecycle receipts."""
from __future__ import annotations

import json
from pathlib import Path
from threading import Lock

from agent.memory_provider import MemoryProvider


class RecorderMemoryProvider(MemoryProvider):
    @property
    def name(self) -> str:
        return "recorder"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self.path = Path(kwargs["hermes_home"]) / "recorder.jsonl"
        self.session_id = session_id
        self.lock = Lock()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return "SHARED_FACT"

    def get_tool_schemas(self) -> list[dict]:
        return []

    def _record(self, hook: str, **payload) -> None:
        with self.lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"hook": hook, "session_id": self.session_id, **payload}) + "\n")

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", **kwargs) -> None:
        self._record("sync_turn", session_id=session_id, user=user_content, assistant=assistant_content)

    def on_session_end(self, messages: list[dict]) -> None:
        self._record("on_session_end", messages=messages)

    def on_pre_compress(self, messages: list[dict]) -> str:
        self._record("on_pre_compress", messages=messages)
        return ""


def register(ctx) -> None:
    ctx.register_memory_provider(RecorderMemoryProvider())
