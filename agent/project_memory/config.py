"""Profile-level service settings for project memory."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

API_KEY_ENV = "PROJECT_HINDSIGHT_API_KEY"


@dataclass(frozen=True)
class ProjectMemoryServiceConfig:
    url: str
    recall_budget: str = "mid"
    recall_max_tokens: int = 1024
    timeout: float = 10.0


def load_service_config(config: Mapping[str, Any]) -> Optional[ProjectMemoryServiceConfig]:
    """Validate an already-loaded profile config; an empty service URL disables it."""
    section = config.get("project_memory")
    if not section:
        return None
    if not isinstance(section, Mapping):
        raise ValueError("project_memory must be a mapping")
    raw_url = section.get("hindsight_url", "")
    if not raw_url:
        return None
    if not isinstance(raw_url, str):
        raise ValueError("project_memory.hindsight_url must be an http(s) URL")
    url = raw_url.strip().rstrip("/")
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("project_memory.hindsight_url must be an http(s) URL")
    budget = section.get("recall_budget", "mid")
    if budget not in ("low", "mid", "high"):
        raise ValueError("project_memory.recall_budget must be low, mid, or high")
    tokens = section.get("recall_max_tokens", 1024)
    timeout = section.get("timeout", 10.0)
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        raise ValueError("project_memory.recall_max_tokens must be a positive integer")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("project_memory.timeout must be a positive number")
    return ProjectMemoryServiceConfig(url, budget, tokens, float(timeout))
