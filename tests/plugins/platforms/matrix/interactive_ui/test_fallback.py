"""Complete answers survive clients without the interactive renderer."""

from copy import deepcopy
from html import unescape
from html.parser import HTMLParser
from importlib.resources import files
import json
import re

import pytest

from plugins.platforms.matrix.adapter_html import markdown_to_matrix_html, sanitize_matrix_html
from plugins.platforms.matrix.interactive_ui.content import build_ui_content
from plugins.platforms.matrix.interactive_ui.fallback import render_fallback_markdown
from plugins.platforms.matrix.interactive_ui.protocol import ParsedUi, parse_interactive_ui

VENDOR = files("plugins.platforms.matrix.interactive_ui").joinpath("vendor/element-web-bcb0801/fixtures/valid")
VALID = sorted(VENDOR.iterdir(), key=lambda path: path.name)


def parsed_fixture(name):
    result = parse_interactive_ui(json.loads(VENDOR.joinpath(name).read_text(encoding="utf-8-sig")))
    assert isinstance(result, ParsedUi), result
    return result.payload


def visible_copy(markdown):
    return unescape(re.sub(r"\\([\\`*_{}\[\]()#+.!|~\-])", r"\1", markdown))


def visible_values(item, context=""):
    """Walk fixture data, excluding wire identity, styling and rendering metadata."""
    hidden = {"id", "type", "ui_id", "version", "revision", "status", "root", "children", "child", "html",
              "direction", "gap", "tone", "size", "kind", "align", "sortable", "ordered", "layer", "style",
              "bind", "bind_selected", "key", "action_id", "include", "single_use", "variant", "multiple",
              "multiline", "display", "level", "from", "to", "mimetype", "width", "height", "file", "state"}
    if isinstance(item, dict):
        if context == "rows":
            for cell in item.values():
                yield from visible_values(cell, "cell")
        else:
            for key, child in item.items():
                if key not in hidden:
                    yield from visible_values(child, key)
    elif isinstance(item, list):
        for child in item:
            yield from visible_values(child, context)
    elif item is not None and not isinstance(item, bool):
        yield str(item)


@pytest.mark.parametrize("without_markdown", [False, True], ids=["installed", "missing"])
@pytest.mark.parametrize("fixture", VALID, ids=lambda path: path.name)
def test_every_valid_fixture_is_complete_and_deterministic(fixture, without_markdown, request):
    if without_markdown:
        request.getfixturevalue("missing_markdown")
    payload = parsed_fixture(fixture.name)
    original = deepcopy(payload)
    markdown = render_fallback_markdown(payload)
    assert markdown == render_fallback_markdown(deepcopy(payload))
    assert payload == original
    visible = visible_copy(markdown)
    for text in visible_values(payload):
        assert text in visible, (fixture.name, text, markdown)
    content = build_ui_content(payload, markdown, sanitize_html=sanitize_matrix_html, thread_metadata=None)
    assert isinstance(content, dict), content
    assert content["body"] == markdown
    assert content["formatted_body"]
    if not without_markdown:
        assert content["formatted_body"] == sanitize_matrix_html(markdown_to_matrix_html(markdown))
    text = []
    reader = HTMLParser()
    reader.handle_data = text.append
    reader.feed(content["formatted_body"])
    reader.close()
    for value in visible_values(payload):
        assert value in "".join(text), (fixture.name, value, content["formatted_body"])


@pytest.mark.parametrize("language,heading,detail,edge,action,branch", [
    ("en", "Cooking knowledge map", "Heat creates flavour", "Sugar → Maillard reaction", "Explain this mechanism", "Nutrition"),
    ("zh", "烹饪知识图谱", "加热产生香气", "糖 → 美拉德反应", "解释这个机制", "营养"),
])
def test_knowledge_maps_keep_relationships_and_hidden_branches(language, heading, detail, edge, action, branch):
    markdown = render_fallback_markdown(parsed_fixture(f"knowledge-map.{language}.json"))
    assert heading in markdown
    assert detail in markdown
    assert edge in markdown
    assert "▸ " + action in markdown
    assert branch in markdown
    assert markdown.count("When “") == 3


def test_table_rows_and_charts_are_associated_with_labels():
    payload = parsed_fixture("table.json")
    table = next(node for node in payload["nodes"] if node["type"] == "table")
    table["columns"] = [{"key": "name", "label": "Food"}, {"key": "mass", "label": "Mass"}]
    table["rows"] = [{"name": "Rice", "mass": 12.5, "note": "dry weight"}, {"name": "Beans", "mass": 0}]
    markdown = render_fallback_markdown(payload)
    assert "- **Rice** — Mass: 12\\.5" in markdown
    assert "note: dry weight" in markdown
    assert "- **Beans** — Mass: 0" in markdown
    assert "|" not in markdown
    scatter = render_fallback_markdown(parsed_fixture("chart-scatter.json"))
    assert "Mass" in scatter and "- A: (1, 2)" in scatter and "- (2, 3)" in scatter


def test_options_current_values_tabs_and_closed_content():
    markdown = render_fallback_markdown(parsed_fixture("controls.json"))
    assert "Plan: basic (current)" in markdown
    assert "- Basic (basic) (current)" in markdown
    assert "- Plus (plus)" in markdown
    assert "Enabled: false (current)" in markdown
    assert "Quantity: 1 (current)" in markdown
    payload = parsed_fixture("layout.json")
    payload["state"]["open"] = False
    markdown = render_fallback_markdown(payload)
    assert "Tab: One (one) (current)" in markdown
    assert "Tab content" in markdown
    assert "Expanded: false" in markdown
    assert "More information" in markdown


def test_conditions_include_negation_membership_and_full_descendants():
    payload = parsed_fixture("knowledge-map.en.json")
    payload["nodes"][0]["visible_when"] = {"state": "branch", "in": ["cooking", "nutrition"], "not": True}
    markdown = render_fallback_markdown(payload)
    assert 'When “Knowledge branch” is not in ["cooking","nutrition"]:' in visible_copy(markdown)
    assert "Heat creates flavour" in markdown
    assert "When “Knowledge branch” is cooking:" in markdown
    assert "When “Knowledge branch” is nutrition:" in markdown


def test_graph_edges_keep_labels_layers_details_and_selection():
    payload = parsed_fixture("knowledge-map.en.json")
    payload["state"]["focus"] = "maillard"
    graph = next(node for node in payload["nodes"] if node["type"] == "graph")
    graph["edges"][0]["label"] = "reacts with"
    markdown = render_fallback_markdown(payload)
    assert "Layers: Ingredients → Mechanisms → Results" in markdown
    assert "Maillard reaction (current) [Mechanisms] — Heat creates flavour" in markdown
    assert "Sugar → Maillard reaction (reacts with)" in markdown


def test_strict_valid_integral_float_heading_levels_render():
    level = 1.0
    raw = {"version": 1, "ui_id": "heading", "revision": 0, "status": "active", "state": {},
           "root": "heading", "nodes": [{"id": "heading", "type": "heading", "level": level, "text": "Heading"}]}
    parsed = parse_interactive_ui(raw, strict=True)
    assert isinstance(parsed, ParsedUi), parsed
    original = deepcopy(parsed.payload)
    assert render_fallback_markdown(parsed.payload) == "#" * int(level) + " Heading"
    assert parsed.payload == original


def test_strict_valid_integral_float_graph_layers_render():
    raw = parsed_fixture("knowledge-map.en.json")
    graph = next(node for node in raw["nodes"] if node["type"] == "graph")
    for node in graph["nodes"]:
        node["layer"] = float(node["layer"])
    parsed = parse_interactive_ui(raw, strict=True)
    assert isinstance(parsed, ParsedUi), parsed
    original = deepcopy(parsed.payload)
    markdown = render_fallback_markdown(parsed.payload)
    assert "Sugar [Ingredients]" in markdown
    assert "Maillard reaction [Mechanisms] — Heat creates flavour" in markdown
    assert "Sugar → Maillard reaction" in markdown
    assert markdown == render_fallback_markdown(parsed.payload)
    assert parsed.payload == original


def test_unknown_widget_and_status_remain_readable():
    payload = parsed_fixture("content.json")
    assert any(node["type"] == "unknown" for node in payload["nodes"])
    payload["status_text"] = "Submitted successfully"
    markdown = render_fallback_markdown(payload, status_line="Expired now")
    assert "Future component" in markdown
    assert "Submitted successfully" in markdown
    assert "Expired now" in markdown


def test_untrusted_markdown_html_and_math_remain_literal():
    payload = parsed_fixture("content.json")
    literal = '# fake\n- list\n1. item\n[x](https://evil.test/)\n<b>tag</b> & &#65; $x$ **bold** `code`\n    indented'
    next(node for node in payload["nodes"] if node["type"] == "text")["text"] = literal
    markdown = render_fallback_markdown(payload)
    assert literal in visible_copy(markdown)
    html = markdown_to_matrix_html(markdown)
    assert '<a href="https://evil.test/">' not in html
    assert "data-mx-maths" not in html
    assert "<b>tag</b>" not in html
    assert "&lt;b&gt;tag&lt;/b&gt;" in html
    assert "<strong>bold</strong>" not in html
    assert "<code>code</code>" not in html
    assert "<h1>fake</h1>" not in html
