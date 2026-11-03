"""Tree and relational validation in Element Web's executable first-error order.

Copyright 2026 sinkerine. SPDX-License-Identifier: MIT
Port source SPDX: AGPL-3.0-only OR LicenseRef-Element-Commercial.
Additional MIT grant and source commit: vendor/element-web-bcb0801/README.md.
"""

from .protocol_json import fail, kind
from .protocol_state import accepts_state_value, binding_kind, child_ids, state_kind, state_string_limit


def index_nodes(payload):
    by_id, indices = {}, {}
    images = 0
    for i, node in enumerate(payload["nodes"]):
        path = f"nodes[{i}]"
        if node["id"] in by_id:
            fail(f"{path}.id", "duplicate_id")
        by_id[node["id"]] = node
        indices[node["id"]] = i
        if node["type"] == "image":
            images += 1
        if node["type"] == "carousel":
            images += len(node["items"])
            seen = set()
            for j, item in enumerate(node["items"]):
                if item["id"] in seen:
                    fail(f"{path}.items[{j}].id", "duplicate_id")
                seen.add(item["id"])
        if images > 24:
            fail(path, "too_many", "At most 24 images, including carousel items")
    return by_id, indices


def reachable_nodes(payload, by_id, indices):
    visited, active, parents = set(), set(), set()

    def walk(node_id, depth, path):
        if node_id not in by_id:
            fail(path, "unknown_ref", f"Missing node id: {node_id}")
        if node_id in active:
            fail(path, "cycle")
        if depth > 12:
            fail(path, "too_deep", "Maximum tree depth is 12, counting the root as one")
        if node_id in visited:
            return
        visited.add(node_id)
        active.add(node_id)
        node = by_id[node_id]
        for j, child in enumerate(child_ids(node)):
            suffix = f"tabs[{j}].child" if node["type"] == "tabs" else f"children[{j}]"
            child_path = f"nodes[{indices[node_id]}].{suffix}"
            if child in active:
                fail(child_path, "cycle")
            if child in parents or child == payload["root"]:
                fail(child_path, "duplicate_id")
            parents.add(child)
            walk(child, depth + 1, child_path)
        active.remove(node_id)

    walk(payload["root"], 1, "root")
    reachable = visited.copy()
    referenced = {child for node in payload["nodes"] for child in child_ids(node)}
    for node in payload["nodes"]:
        if node["id"] not in visited and node["id"] not in referenced:
            walk(node["id"], 1, f"nodes[{indices[node['id']]}]")
    for node in payload["nodes"]:
        if node["id"] not in visited:
            walk(node["id"], 1, f"nodes[{indices[node['id']]}]")
    return reachable


def validate_chart(node, path):
    if node["kind"] == "pie" and len(node["series"]) != 1:
        fail(f"{path}.series", "bad_value", "Pie charts require exactly one series")
    count = 0
    for j, series in enumerate(node["series"]):
        if "values" in series:
            if "categories" not in node or len(series["values"]) != len(node["categories"]):
                fail(f"{path}.series[{j}].values", "bad_value", "Series values must match the category count")
            count += len(series["values"])
        else:
            count += len(series["points"]) * 2
    if count > 800:
        fail(f"{path}.series", "too_many", "At most 800 values or 400 scatter points across all series")


def validate_table(node, path):
    if len({c["key"] for c in node["columns"]}) != len(node["columns"]):
        fail(f"{path}.columns", "duplicate_id")
    for j, row in enumerate(node["rows"]):
        for column in node["columns"]:
            value = row.get(column["key"])
            if value is not None and column.get("kind") == "number" and kind(value) != "number":
                fail(f"{path}.rows[{j}].{column['key']}", "type_mismatch")


def validate_graph(node, path, reachable, reference, retained):
    ids = set()
    for j, graph_node in enumerate(node["nodes"]):
        if graph_node["id"] in ids:
            fail(f"{path}.nodes[{j}].id", "duplicate_id")
        ids.add(graph_node["id"])
        if graph_node["layer"] >= len(node["layers"]):
            fail(f"{path}.nodes[{j}].layer", "bad_value")
    for j, edge in enumerate(node["edges"]):
        for key in ("from", "to"):
            if edge[key] not in ids:
                fail(f"{path}.edges[{j}].{key}", "unknown_ref", f"Missing graph node id: {edge[key]}")
    if reachable and "bind_selected" in node:
        reference(node["bind_selected"], f"{path}.bind_selected", "string")
        if not accepts_state_value(retained, node["bind_selected"], retained["state"][node["bind_selected"]]):
            fail(f"{path}.bind_selected", "bad_value")


def validate_control(node, path):
    if node["type"] == "number_input":
        if "min" in node and "max" in node and node["min"] > node["max"]:
            fail(path, "bad_value", "min must not exceed max")
        if node.get("display") == "slider" and ("min" not in node or "max" not in node):
            fail(path, "bad_value", "Sliders require min and max")
    if node["type"] in ("select", "choice", "tabs"):
        name = "tabs" if node["type"] == "tabs" else "options"
        if len({option["value"] for option in node[name]}) != len(node[name]):
            fail(f"{path}.{name}", "duplicate_id")


def validate_relations(payload):
    by_id, indices = index_nodes(payload)
    reachable = reachable_nodes(payload, by_id, indices)
    retained = {**payload, "nodes": [node for node in payload["nodes"] if node["id"] in reachable]}

    def reference(key, path, expected=None):
        if key not in payload["state"]:
            fail(path, "unknown_ref", f"Missing state key: {key}")
        if expected and state_kind(retained, key) != expected:
            fail(path, "type_mismatch", f"State key {key} must have kind {expected}")

    def action(value, path):
        if value["type"] == "submit":
            for i, key in enumerate(value.get("include", [])):
                reference(key, f"{path}.include[{i}]")
        if value["type"] in ("set_state", "toggle_state"):
            reference(value["key"], f"{path}.key", "boolean" if value["type"] == "toggle_state" else None)
            if value["type"] == "set_state" and not accepts_state_value(retained, value["key"], value["value"]):
                fail(f"{path}.value", "type_mismatch")

    for i, node in enumerate(payload["nodes"]):
        path = f"nodes[{i}]"
        is_reachable = node["id"] in reachable
        validate_binding(node, path, is_reachable, retained, reference)
        validate_control(node, path)
        if is_reachable and node["type"] == "button":
            action(node["action"], f"{path}.action")
        if is_reachable and node["type"] == "carousel":
            for j, item in enumerate(node["items"]):
                if "action" in item:
                    action(item["action"], f"{path}.items[{j}].action")
        validator = {"chart": validate_chart, "table": validate_table}.get(node["type"])
        if validator:
            validator(node, path)
        if node["type"] == "graph":
            validate_graph(node, path, is_reachable, reference, retained)
    for key, value in payload["state"].items():
        if not accepts_state_value(retained, key, value):
            code = "too_long" if type(value) is str and len(value) > state_string_limit(retained, key) else "bad_value"
            fail(f"state.{key}", code)
    return {node_id: node for node_id, node in by_id.items() if node_id in reachable}


def validate_binding(node, path, reachable, retained, reference):
    condition = node.get("visible_when")
    if reachable and condition:
        reference(condition["state"], f"{path}.visible_when.state")
        values = [condition["equals"]] if "equals" in condition else condition.get("in", [])
        if any(v is not None and kind(v) != state_kind(retained, condition["state"]) for v in values):
            fail(f"{path}.visible_when", "type_mismatch")
    if reachable and "bind" in node:
        reference(node["bind"], f"{path}.bind", binding_kind(node))
        if not accepts_state_value(retained, node["bind"], retained["state"][node["bind"]]):
            fail(f"{path}.bind", "bad_value")
