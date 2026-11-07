"""Synchronous Hindsight HTTP implementation of the project memory contract."""
from __future__ import annotations

from typing import Optional, Sequence
from urllib.parse import quote

import httpx

from agent.project_memory.backend import BackendError, RecallHit, RetainItem
from agent.project_memory.config import API_KEY_ENV, ProjectMemoryServiceConfig
from agent.secret_scope import get_secret
from agent.ssl_verify import resolve_httpx_verify


class HindsightBackend:
    def __init__(self, service: ProjectMemoryServiceConfig, *, api_key: str = "",
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        self.service = service
        self._client = httpx.Client(
            timeout=service.timeout, transport=transport,
            verify=resolve_httpx_verify(base_url=service.url),
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )

    def _request(self, method: str, bank_id: str, suffix: str = "", *,
                 body: Optional[dict] = None, missing_ok: bool = False) -> httpx.Response:
        url = f"{self.service.url}/v1/default/banks/{quote(bank_id, safe='')}{suffix}"
        try:
            response = self._client.request(method, url, json=body)
        except httpx.HTTPError as exc:
            raise BackendError("Project memory request failed", status=None) from exc
        if missing_ok and response.status_code == 404:
            return response
        if not 200 <= response.status_code < 300:
            raise BackendError("Project memory request failed", status=response.status_code)
        return response

    def ensure_bank(self, bank_id: str, *, name: str) -> None:
        self._request("PUT", bank_id, body={"name": name})

    def bank_exists(self, bank_id: str) -> bool:
        return self._request("GET", bank_id, "/config", missing_ok=True).status_code != 404

    def recall(self, bank_id: str, query: str) -> list[RecallHit]:
        response = self._request("POST", bank_id, "/memories/recall", missing_ok=True, body={
            "query": query, "budget": self.service.recall_budget,
            "max_tokens": self.service.recall_max_tokens,
        })
        if response.status_code == 404:
            return []
        try:
            return [RecallHit(bank_id=bank_id, text=item["text"], id=item.get("id", ""),
                              kind=item.get("type", ""), occurred_at=item.get("occurred_start", ""))
                    for item in response.json()["results"]]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise BackendError("Invalid project memory recall response", status=response.status_code) from exc

    def retain(self, bank_id: str, items: Sequence[RetainItem]) -> None:
        payload = [{"content": item.content, "context": item.context,
                    "document_id": item.document_id, "tags": list(item.tags),
                    "metadata": dict(item.metadata),
                    **({"timestamp": item.timestamp} if item.timestamp else {})}
                   for item in items]
        self._request("POST", bank_id, "/memories", body={"items": payload, "async": True})

    def close(self) -> None:
        self._client.close()


def backend_for_current_profile(service: ProjectMemoryServiceConfig) -> HindsightBackend:
    """Construct in the caller's profile scope, binding its credential to this client."""
    return HindsightBackend(service, api_key=get_secret(API_KEY_ENV, "") or "")
