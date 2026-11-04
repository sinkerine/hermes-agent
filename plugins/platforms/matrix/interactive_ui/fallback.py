"""Complete deterministic Markdown for clients without interactive UI support."""

from collections.abc import Mapping
from typing import Any

from ._fallback_nodes import NODE_RENDERERS, escape, fields, value
from .protocol import child_ids


class _Renderer:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.state = payload["state"]
        self.nodes = {node["id"]: node for node in payload["nodes"]}

    def state_label(self, key: str) -> str:
        return next((node["label"] for node in self.nodes.values()
                     if node.get("bind") == key and "label" in node), key)

    def condition(self, condition: Mapping[str, Any]) -> str:
        key = condition["state"]
        comparison = "equals" if "equals" in condition else "in"
        operator = {("equals", False): "is", ("equals", True): "is not",
                    ("in", False): "is in", ("in", True): "is not in"}[(comparison, condition.get("not", False))]
        return f'When “{escape(self.state_label(key))}” {operator} {value(condition[comparison])}:'

    def render(self, node_id: str) -> list[str]:
        node = self.nodes[node_id]
        lines = []
        if "visible_when" in node:
            lines.append(self.condition(node["visible_when"]))
        lines.extend(NODE_RENDERERS.get(node["type"], NODE_RENDERERS["unknown"])(node, self))
        if node["type"] != "tabs":
            for child in child_ids(node):
                lines.extend(self.render(child))
        if node["type"] == "form":
            lines.append("▸ " + escape(node["submit_label"]))
        return lines


def render_fallback_markdown(payload: Mapping[str, Any], *, status_line: str | None = None) -> str:
    """Render a parsed v1 payload, including every inactive branch and control."""
    renderer = _Renderer(payload)
    lines = fields(payload, ("title",))
    lines.extend(escape(line) for line in (payload.get("status_text"), status_line) if line)
    lines.extend(renderer.render(payload["root"]))
    return "\n\n".join(lines)


def render_fallback_html(markdown_text: str) -> str:
    """Use the adapter pipeline, or preserve generated literals without Markdown."""
    from ..adapter_html import markdown_to_matrix_html
    from ._fallback_html import generated_markdown_to_html

    try:
        __import__("markdown")
    except ImportError:
        return generated_markdown_to_html(markdown_text)
    return markdown_to_matrix_html(markdown_text)
