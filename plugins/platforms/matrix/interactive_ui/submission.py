"""Server-side submit sources and Element Web state.ts value semantics.

Copyright 2026 sinkerine. SPDX-License-Identifier: MIT
Port source: apps/web/src/interactive-ui/state.ts at
bcb080171cb312feef37e63007c7ca8cc0a8b77f.
Original SPDX: AGPL-3.0-only OR LicenseRef-Element-Commercial.
Additional MIT grant: vendor/element-web-bcb0801/README.md.
"""

from dataclasses import dataclass

from .protocol import ParsedUi, accepts_state_value, child_ids
from .protocol_json import trim


@dataclass(frozen=True)
class SubmitSource:
    node_id: str
    item_id: str | None
    action_id: str
    keys: frozenset[str]
    single_use: bool
    label: str


def _form_keys(parsed: ParsedUi, node: dict) -> frozenset[str]:
    pending = list(child_ids(node))
    visited = set()
    keys = set()
    while pending:
        node_id = pending.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        descendant = parsed.nodes_by_id[node_id]
        # Element collects bind, not graph bind_selected, even in hidden branches.
        if "bind" in descendant:
            keys.add(descendant["bind"])
        pending.extend(child_ids(descendant))
    return frozenset(keys)


def _node_sources(parsed: ParsedUi, node: dict) -> list[SubmitSource]:
    if node["type"] == "form":
        return [SubmitSource(node["id"], None, node["action_id"], _form_keys(parsed, node),
                             node.get("single_use", True), node["submit_label"])]
    actions = []
    if node["type"] == "button":
        actions.append((None, node["label"], node["action"]))
    if node["type"] == "carousel":
        actions.extend((item["id"], item["title"], item.get("action", {})) for item in node["items"])
    return [SubmitSource(node["id"], item_id, action["action_id"], frozenset(action.get("include", [])),
                         action.get("single_use", False), label)
            for item_id, label, action in actions if action.get("type") == "submit"]


def submit_sources(parsed: ParsedUi) -> dict[str, SubmitSource]:
    """Reachable submit actions only; ambiguous action IDs raise ValueError.

    Visibility and status are not source filters. Required descendants can be
    hidden, and durable duplicate detection must precede inactive rejection.
    """
    sources = {}
    for node in parsed.nodes_by_id.values():
        for source in _node_sources(parsed, node):
            if source.action_id in sources:
                raise ValueError("Submit action_id must identify exactly one source")
            sources[source.action_id] = source
    return sources


def validate_action_values(parsed: ParsedUi, source: SubmitSource, values: object) -> list[str] | None:
    """Return invalid keys in stable order, or None for an exact valid key set.

    Unlike the client's local state map, a wire submission cannot contain extra
    keys. A non-object returns all expected keys (or [] for an empty source).
    """
    if type(values) is not dict:
        return sorted(source.keys)
    invalid = set(source.keys.symmetric_difference(values))
    required = {node["bind"] for node in parsed.nodes_by_id.values()
                if node["type"] == "text_input" and node.get("required")}
    for key in source.keys.intersection(values):
        value = values[key]
        if (not accepts_state_value(parsed.payload, key, value)
                or (key in required and (type(value) is not str or trim(value) == ""))):
            invalid.add(key)
    # Non-string object keys are not JSON keys; avoid comparing mixed key types.
    if any(type(key) is not str for key in invalid):
        return sorted(source.keys)
    return sorted(invalid) if invalid else None
