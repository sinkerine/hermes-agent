"""Producer additions preserve the client's first errors and enforce wire budgets."""

from time import perf_counter

import pytest

from plugins.platforms.matrix.interactive_ui.protocol import ParsedUi, canonical_json_bytes, parse_interactive_ui
from tests.plugins.platforms.matrix.interactive_ui.test_protocol_conformance import assert_error, basic


def test_strict_unreachable_node_uses_original_index():
    raw = basic([{"id": "unused", "type": "divider"}, {"id": "root", "type": "divider"}])
    assert isinstance(parse_interactive_ui(raw), ParsedUi)
    assert_error(raw, "nodes[0]", "unknown_ref", strict=True)


def test_strict_unknown_node_type():
    raw = basic([{"id": "root", "type": "future", "fallback": "Future"}])
    assert isinstance(parse_interactive_ui(raw), ParsedUi)
    assert_error(raw, "nodes[0].type", "bad_type", strict=True)


def submit_node(node_type, node_id):
    action = {"type": "submit", "action_id": "same"}
    return {
        "button": {"id": node_id, "type": "button", "label": "Send", "action": action},
        "form": {"id": node_id, "type": "form", "children": [], "submit_label": "Send", "action_id": "same"},
        "carousel": {"id": node_id, "type": "carousel", "label": "Items", "items": [{"id": "item", "title": "Item", "image": {
            "alt": "Picture", "url": "mxc://example.org/image", "mimetype": "image/png"}, "action": action}]},
    }[node_type]


@pytest.mark.parametrize("first", ["button", "form", "carousel"])
@pytest.mark.parametrize("second,path", [("button", "nodes[2].action.action_id"), ("form", "nodes[2].action_id"), ("carousel", "nodes[2].items[0].action.action_id")])
def test_duplicate_submit_ids_across_source_types(first, second, path):
    raw = basic([{"id": "root", "type": "stack", "direction": "vertical", "children": ["a", "b"]}, submit_node(first, "a"), submit_node(second, "b")])
    assert isinstance(parse_interactive_ui(raw), ParsedUi)
    assert_error(raw, path, "duplicate_id", strict=True)


def test_duplicate_submit_ids_within_one_carousel():
    node = submit_node("carousel", "root")
    node["items"].append({**node["items"][0], "id": "other"})
    assert_error(basic([node]), "nodes[0].items[1].action.action_id", "duplicate_id", strict=True)


def test_client_errors_precede_all_strict_checks():
    raw = basic([{"id": "root", "type": "future"}, {"id": "unused", "type": "toggle", "label": "Toggle", "bind": "missing"}], {"x": "x" * 201})
    raw["ignored"] = "x" * 40_000
    for strict in (False, True):
        assert_error(raw, "state.x", "too_long", strict)
    raw["nodes"][1].pop("label")
    for strict in (False, True):
        assert_error(raw, "nodes[1].label", "missing_field", strict)


def padded_payload(limit, nodes=1):
    raw = basic()
    if nodes > 1:
        raw["nodes"] = [{"id": "root", "type": "stack", "direction": "vertical", "children": [f"n{i}" for i in range(nodes - 1)]}]
        raw["nodes"].extend({"id": f"n{i}", "type": "divider"} for i in range(nodes - 1))
    raw["extra"] = ""
    remaining = limit - len(canonical_json_bytes(raw))
    raw["extra"] = "中" * (remaining // 3) + "x" * (remaining % 3)
    assert len(canonical_json_bytes(raw)) == limit
    return raw


@pytest.mark.parametrize("limit,strict", [(40_000, True), (60_000, False)])
def test_byte_boundaries_include_ignored_properties(limit, strict):
    raw = padded_payload(limit, nodes=300)
    assert isinstance(parse_interactive_ui(raw, strict=strict), ParsedUi)
    raw["extra"] += "x"
    assert_error(raw, "", "too_large", strict)


def test_three_hundred_nodes_performance():
    raw = padded_payload(60_000, nodes=300)
    assert isinstance(parse_interactive_ui(raw), ParsedUi)
    start = perf_counter()
    assert isinstance(parse_interactive_ui(raw), ParsedUi)
    elapsed = perf_counter() - start
    # Keep noisy-host timing out of pass/fail; report the plan's 200 ms target.
    print(f"300 nodes / 60000 bytes: {elapsed * 1000:.2f} ms (target <200 ms)")
    assert elapsed < 2


def test_canonical_bytes_are_compact_utf8_and_forbid_nan():
    assert canonical_json_bytes({"text": "中文", "value": 1.0}) == '{"text":"中文","value":1.0}'.encode()
    with pytest.raises(ValueError):
        canonical_json_bytes({"value": float("nan")})


def test_client_uses_js_number_byte_count():
    raw = padded_payload(60_000)
    # JS spells 1.0 as 1; Python's canonical producer bytes deliberately spell 1.0.
    raw["revision"] = 1.0
    assert isinstance(parse_interactive_ui(raw), ParsedUi)
    raw["extra"] += "x"
    assert_error(raw, "", "too_large")
    for number, javascript_spelling in [(1e-6, "0.000001"), (1e20, "100000000000000000000"),
                                         (1e21, "1e+21"), (1e-7, "1e-7"), (-0.0, "0")]:
        raw = basic()
        raw["extra"] = ""
        base_size = len(canonical_json_bytes(raw))
        raw["ignored_number"] = number
        added_size = len(',"ignored_number":') + len(javascript_spelling)
        raw["extra"] = "x" * (60_000 - base_size - added_size)
        assert isinstance(parse_interactive_ui(raw), ParsedUi)
        raw["extra"] += "x"
        assert_error(raw, "", "too_large")
