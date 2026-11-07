"""Provider-independent contracts for project memory banks."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Sequence


@dataclass(frozen=True)
class RecallHit:
    bank_id: str
    text: str
    id: str = ""
    kind: str = ""
    occurred_at: str = ""


@dataclass(frozen=True)
class RetainItem:
    content: str
    context: str
    document_id: str
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)
    timestamp: str = ""


class BackendError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class ProjectMemoryBackend(Protocol):
    def ensure_bank(self, bank_id: str, *, name: str) -> None: ...
    def bank_exists(self, bank_id: str) -> bool: ...
    def recall(self, bank_id: str, query: str) -> list[RecallHit]: ...
    def retain(self, bank_id: str, items: Sequence[RetainItem]) -> None: ...
    def close(self) -> None: ...
