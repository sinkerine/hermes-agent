"""Element Web state binding semantics, shared by parsing and submissions.

Copyright 2026 sinkerine. SPDX-License-Identifier: MIT
Port source SPDX: AGPL-3.0-only OR LicenseRef-Element-Commercial.
Additional MIT grant and source commit: vendor/element-web-bcb0801/README.md.
"""

import math

from .protocol_json import FORBIDDEN_CONTROLS, kind


def child_ids(node):
    if "children" in node:
        return node["children"]
    if node["type"] == "tabs":
        return [tab["child"] for tab in node["tabs"]]
    return []


def state_kind(payload, key):
    if key not in payload["state"]:
        return "undefined"
    value = payload["state"][key]
    if value is not None:
        return kind(value)
    for node in payload["nodes"]:
        if node.get("bind_selected") == key:
            return "string"
        if node.get("bind") == key:
            return binding_kind(node)
    return "null"


def binding_kind(node):
    return {"number_input": "number", "toggle": "boolean", "collapsible": "boolean"}.get(
        node["type"], "array" if node["type"] == "choice" and node.get("multiple") else "string",
    )


def state_string_limit(payload, key):
    limits = [node["max_length"] for node in payload["nodes"] if node["type"] == "text_input" and node.get("bind") == key]
    return min(limits, default=200)


def accepts_scalar(payload, key, value):
    value_kind = kind(value)
    if value_kind == "number":
        try:
            return math.isfinite(value)
        except OverflowError:
            return False
    if value_kind == "string":
        return len(value) <= state_string_limit(payload, key) and not FORBIDDEN_CONTROLS.search(value)
    if value_kind == "array":
        return len(value) <= 50 and all(type(v) is str and len(v) <= 200 and not FORBIDDEN_CONTROLS.search(v) for v in value) and len(set(value)) == len(value)
    return True


def accepts_binding(node, value):
    node_type = node["type"]
    if node_type in ("tabs", "choice", "select"):
        options = node["tabs"] if node_type == "tabs" else node["options"]
        values = value if type(value) is list else [value]
        return all(any(option["value"] == v for option in options) for v in values)
    if node_type == "text_input":
        return type(value) is str and len(value) <= node["max_length"]
    if node_type == "number_input":
        return kind(value) == "number" and ("min" not in node or value >= node["min"]) and ("max" not in node or value <= node["max"])
    return True


def accepts_state_value(payload, key, value):
    if key not in payload["state"]:
        return False
    expected = state_kind(payload, key)
    if value is None:
        return expected in ("string", "number", "null")
    if kind(value) != expected or not accepts_scalar(payload, key, value):
        return False
    for node in payload["nodes"]:
        if node.get("bind") == key and not accepts_binding(node, value):
            return False
    graphs = [node for node in payload["nodes"] if node["type"] == "graph" and node.get("bind_selected") == key]
    return not graphs or any(any(n["id"] == value for n in graph["nodes"]) for graph in graphs)
