"""Table-driven rendering of the v1 node catalog."""

import json
import re


def escape(text):
    """Keep untrusted text literal, including HTML and the adapter's math syntax."""
    text = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("$", "&#36;")
    text = re.sub(r"([\\`*_{}\[\]()#+.!|~\-])", r"\\\1", text)
    # The adapter's Markdown pre-sanitizer removes attribute-like text even outside HTML.
    return re.sub(r"(?m)^[ \t]+", lambda m: "&#32;" * len(m[0]), text).replace("=", "&#61;")


def value(item):
    return escape(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, separators=(",", ":")))


def fields(node, keys):
    return [escape(node[key]) for key in keys if key in node]


def _plain(node, renderer):
    return fields(node, ("title", "label", "text"))


def _heading(node, renderer):
    return ["#" * int(node["level"]) + " " + escape(node["text"])]


def _unknown(node, renderer):
    return [escape(node.get("fallback", "Unsupported component"))]


def _items(node, renderer):
    lines = []
    for index, item in enumerate(node["items"], 1):
        prefix = f"{index}. " if node.get("ordered") else "- "
        parts = fields(item, ("label", "value", "text", "secondary"))
        lines.append(prefix + " — ".join(parts))
    return lines


def _image(node, renderer):
    source = node.get("url") or node["file"]["url"]
    return [f'Image: {escape(node["alt"])} ({escape(source)})']


def _link(node, renderer):
    return [f'{escape(node["label"])} — {escape(node["url"])}']


def _action(action, renderer):
    kind = action["type"]
    if kind == "open_url":
        return " — " + escape(action["url"])
    if kind == "set_state":
        return f' — {escape(renderer.state_label(action["key"]))} = {value(action["value"])}'
    if kind == "toggle_state":
        return " — Toggle “" + escape(renderer.state_label(action["key"])) + "”"
    return ""


def _button(node, renderer):
    return ["▸ " + escape(node["label"]) + _action(node["action"], renderer)]


def _carousel(node, renderer):
    lines = fields(node, ("label",))
    for item in node["items"]:
        lines.extend(fields(item, ("title", "subtitle")))
        lines.extend(escape(text) for text in item.get("meta", []))
        lines.extend(fields(item.get("badge", {}), ("text",)))
        lines.extend(_image(item["image"], renderer))
        if "action" in item:
            lines.append("▸ " + escape(item["title"]) + _action(item["action"], renderer))
    return lines


def _table(node, renderer):
    columns = node["columns"]
    lines = fields(node, ("caption",))
    lines.append("Columns: " + " · ".join(escape(column["label"]) for column in columns))
    for row in node["rows"]:
        first, *rest = columns
        lead = f'**{value(row.get(first["key"]))}**'
        cells = [f'{escape(column["label"])}: {value(row.get(column["key"]))}' for column in rest]
        # The wire parser permits cells without a declared column; keep their data too.
        declared = {column["key"] for column in columns}
        cells.extend(f"{escape(key)}: {value(cell)}" for key, cell in row.items() if key not in declared)
        lines.append("- " + lead + (" — " + " · ".join(cells) if cells else ""))
    return lines


def _chart(node, renderer):
    lines = fields(node, ("title",))
    lines.extend(f"{label}: {escape(node[key])}" for key, label in
                 (("x_label", "X"), ("y_label", "Y"), ("unit", "Unit")) if key in node)
    # Categories remain visible even when every series is empty.
    lines.extend("Category: " + escape(category) for category in node.get("categories", []))
    for series in node["series"]:
        lines.append(escape(series["name"]))
        if node["kind"] == "scatter":
            for point in series["points"]:
                label = escape(point["label"]) + ": " if "label" in point else ""
                lines.append(f'- {label}({value(point["x"])}, {value(point["y"])})')
        else:
            lines.extend(f"- {escape(category)}: {value(number)}" for category, number in
                         zip(node["categories"], series["values"]))
    return lines


def _graph(node, renderer):
    labels = {item["id"]: item["label"] for item in node["nodes"]}
    lines = ["Layers: " + " → ".join(escape(layer["label"]) for layer in node["layers"])]
    selected = renderer.state.get(node.get("bind_selected"))
    for item in node["nodes"]:
        layer = node["layers"][int(item["layer"])]["label"]
        current = " (current)" if item["id"] == selected else ""
        detail = " — " + escape(item["detail"]) if "detail" in item else ""
        lines.append(f'- {escape(item["label"])}{current} [{escape(layer)}]{detail}')
    for edge in node["edges"]:
        label = " (" + escape(edge["label"]) + ")" if "label" in edge else ""
        lines.append(f'- {escape(labels[edge["from"]])} → {escape(labels[edge["to"]])}{label}')
    if "bind_selected" in node:
        lines.append("Current selection: " + value(selected))
    return lines


def _control(node, renderer):
    current = renderer.state[node["bind"]]
    lines = [escape(node.get("label", node["bind"])) + ": " + value(current) + " (current)"]
    for option in node.get("options", []):
        selected = option["value"] in current if isinstance(current, list) else option["value"] == current
        marker = " (current)" if selected else ""
        lines.append(f'- {escape(option["label"])} ({escape(option["value"])}){marker}')
    lines.extend(f"{key}: {value(node[key])}" for key in
                 ("placeholder", "min", "max", "step", "max_length", "required") if key in node)
    return lines


def _tabs(node, renderer):
    lines = ["Current tab: " + value(renderer.state[node["bind"]])]
    for tab in node["tabs"]:
        marker = " (current)" if tab["value"] == renderer.state[node["bind"]] else ""
        lines.append(f'Tab: {escape(tab["label"])} ({escape(tab["value"])}){marker}')
        lines.extend(renderer.render(tab["child"]))
    return lines


def _collapsible(node, renderer):
    lines = fields(node, ("label",))
    if "bind" in node:
        lines.append("Expanded: " + value(renderer.state[node["bind"]]))
    return lines


NODE_RENDERERS = {
    "stack": _plain, "card": _plain, "tabs": _tabs, "collapsible": _collapsible,
    "divider": lambda node, renderer: ["---"], "heading": _heading,
    "text": _plain, "rich_text": _plain, "badge": _plain,
    "key_value": _items, "list": _items, "image": _image, "carousel": _carousel,
    "link": _link, "table": _table, "chart": _chart, "graph": _graph,
    "button": _button, "select": _control, "choice": _control, "toggle": _control,
    "text_input": _control, "number_input": _control, "form": _plain, "unknown": _unknown,
}
