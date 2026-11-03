"""Behavioral conformance with the pinned Element Web v1 contract."""

from copy import deepcopy
from importlib.resources import files
import json

import pytest

from plugins.platforms.matrix.interactive_ui.protocol import (
    InteractiveUiError, ParsedUi, accepts_state_value, child_ids, parse_interactive_ui, state_kind,
)

VENDOR = files("plugins.platforms.matrix.interactive_ui").joinpath("vendor/element-web-bcb0801")
VALID = sorted(VENDOR.joinpath("fixtures/valid").iterdir(), key=lambda p: p.name)
INVALID = sorted((p for p in VENDOR.joinpath("fixtures/invalid").iterdir() if not p.name.endswith(".expected.json")), key=lambda p: p.name)


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def basic(nodes=None, state=None):
    return {"version": 1, "ui_id": "test", "revision": 0, "status": "active", "state": state or {},
            "root": "root", "nodes": nodes or [{"id": "root", "type": "divider"}]}


def assert_error(raw, path, code, strict=False):
    result = parse_interactive_ui(raw, strict=strict)
    assert isinstance(result, InteractiveUiError), result
    assert (result.path, result.code) == (path, code)
    return result


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("fixture", VALID, ids=lambda p: p.name)
def test_valid_fixtures(fixture, strict):
    raw = load(fixture)
    original = deepcopy(raw)
    # This fixture intentionally exercises forward compatibility. Producers reject its future type.
    if strict and fixture.name == "content.json":
        assert_error(raw, "nodes[9].type", "bad_type", strict=True)
        return
    result = parse_interactive_ui(raw, strict=strict)
    assert isinstance(result, ParsedUi), result
    assert raw == original
    assert set(result.nodes_by_id) == {node["id"] for node in result.payload["nodes"]}
    assert parse_interactive_ui(result.payload, strict=strict) == result
    assert all(child in result.nodes_by_id for node in result.payload["nodes"] for child in child_ids(node))


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("fixture", INVALID, ids=lambda p: p.name)
def test_invalid_fixtures(fixture, strict):
    expected = load(fixture.parent.joinpath(fixture.name.replace(".json", ".expected.json")))
    assert_error(load(fixture), expected["path"], expected["code"], strict)


@pytest.mark.parametrize("revision,accepted", [(True, False), (False, False), (1.0, True), (9007199254740991, True), (9007199254740992, False)])
def test_safe_integer_revision(revision, accepted):
    result = parse_interactive_ui({**basic(), "revision": revision})
    assert isinstance(result, ParsedUi) is accepted


@pytest.mark.parametrize("version", [True, "1", None, 2])
def test_version_has_no_boolean_coercion(version):
    assert_error({**basic(), "version": version}, "version", "bad_version")


@pytest.mark.parametrize("space,trimmed", [("\ufeff", True), ("\u00a0", True), ("\u2003", True), ("\x85", False), ("\u001c", None)])
def test_js_trim_and_controls(space, trimmed):
    raw = basic([{"id": "root", "type": "text", "text": space + "text" + space}])
    result = parse_interactive_ui(raw)
    if trimmed is None:
        assert_error(raw, "nodes[0].text", "bad_value")
    else:
        assert isinstance(result, ParsedUi)
        assert result.payload["nodes"][0]["text"] == ("text" if trimmed else space + "text" + space)


def test_code_points_and_parsed_versus_raw_state_lengths():
    raw = basic([{"id": "root", "type": "text_input", "label": "Text", "bind": "text", "max_length": 2}], {"text": "  😀😀  "})
    parsed = parse_interactive_ui(raw)
    assert isinstance(parsed, ParsedUi)
    assert parsed.payload["state"]["text"] == "😀😀"
    assert accepts_state_value(parsed.payload, "text", "😀😀")
    assert not accepts_state_value(parsed.payload, "text", "  😀😀  ")


@pytest.mark.parametrize("state,expected", [({"10": [], "2": 3, "bad-key": 1}, ("state.bad-key", "bad_id")),
                                           ({"10": float("inf"), "2": float("nan")}, ("state.2", "bad_value"))])
def test_js_object_key_first_error(state, expected):
    assert_error(basic(state=state), *expected)


@pytest.mark.parametrize("url,accepted", [
    ("https://example.org/path?q=1", True), ("https://例え.テスト/", True),
    ("https://@example.org", True), ("https://user@example.org", False),
    ("https://user:pw@example.org", False), ("https://example.org:65536", False),
    ("https://[::1]/", True), ("https://256.0.0.1/", False),
    ("https://0x7f.1/", True), ("https://09/", False), ("https://example.org/a\u00a0b", False),
    ("http://example.org", False), ("https://", False), ("https://exa%mple.org", False),
    ("https://a\u200cb.com", False), ("https://\u0300.com", False),
    ("https://\u200b.com", True), ("https://\u3002", True),
    ("https://💩.la", True), ("https://ß.de", True), ("https://foo..com", True),
    ("https://" + "a" * 64 + ".com", True),
])
def test_https_url_contract(url, accepted):
    result = parse_interactive_ui(basic([{"id": "root", "type": "link", "label": "Link", "url": url}]))
    assert isinstance(result, ParsedUi) is accepted, result


def test_client_discards_unknown_fields_and_unreachable_nodes():
    raw = basic([{"id": "root", "type": "future", "fallback": " Future ", "arbitrary": {}}, {"id": "unused", "type": "divider"}])
    raw["ignored"] = {"anything": True}
    parsed = parse_interactive_ui(raw)
    assert isinstance(parsed, ParsedUi)
    assert "ignored" not in parsed.payload
    assert parsed.payload["nodes"] == [{"id": "root", "type": "unknown", "fallback": "Future"}]


@pytest.mark.parametrize("node_type,extra,expected,nullable", [
    ("text_input", {"max_length": 2000}, "string", True),
    ("number_input", {}, "number", True), ("toggle", {}, "boolean", False),
    ("choice", {"multiple": True, "options": [{"value": "a", "label": "A"}]}, "array", False),
])
def test_null_binding_rulings(node_type, extra, expected, nullable):
    raw = basic([{"id": "root", "type": node_type, "bind": "value", "label": "Value", **extra}], {"value": None})
    assert state_kind(raw, "value") == expected
    assert accepts_state_value(raw, "value", None) is nullable
    assert isinstance(parse_interactive_ui(raw), ParsedUi) is nullable


def test_shared_bindings_require_every_control_limit():
    nodes = [{"id": "root", "type": "stack", "direction": "vertical", "children": ["a", "b"]},
             {"id": "a", "type": "number_input", "label": "A", "bind": "n", "min": 0, "max": 5},
             {"id": "b", "type": "number_input", "label": "B", "bind": "n", "min": 3, "max": 8}]
    raw = basic(nodes, {"n": None})
    assert isinstance(parse_interactive_ui(raw), ParsedUi)
    assert accepts_state_value(raw, "n", 4)
    assert not accepts_state_value(raw, "n", 2)
    assert not accepts_state_value(raw, "n", True)
    assert not accepts_state_value(raw, "n", float("nan"))
    nodes[2] = {"id": "b", "type": "toggle", "label": "B", "bind": "n"}
    assert_error(raw, "nodes[2].bind", "type_mismatch")


def test_graph_selection_uses_union_of_bound_graphs():
    graph = lambda name: {"id": name, "type": "graph", "layers": [{"label": "Layer"}], "nodes": [{"id": name, "label": name, "layer": 0}], "edges": [], "bind_selected": "selected"}
    raw = basic([{"id": "root", "type": "stack", "direction": "vertical", "children": ["a", "b"]}, graph("a"), graph("b")], {"selected": "b"})
    assert isinstance(parse_interactive_ui(raw), ParsedUi)
    for value in ("a", "b", None):
        assert accepts_state_value(raw, "selected", value)
    assert not accepts_state_value(raw, "selected", "missing")


def test_scatter_coordinate_budget():
    raw = basic([{"id": "root", "type": "chart", "kind": "scatter", "series": [
        {"name": "A", "points": [{"x": 1, "y": 2}] * 200},
        {"name": "B", "points": [{"x": 3, "y": 4}] * 200}]}])
    assert isinstance(parse_interactive_ui(raw, strict=True), ParsedUi)
    raw["nodes"][0]["series"][1]["points"].append({"x": 5, "y": 6})
    assert_error(raw, "nodes[0].series", "too_many")
