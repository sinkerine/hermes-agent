"""Submission source discovery and exact server-side Element value validation."""

from copy import deepcopy

import pytest

from plugins.platforms.matrix.interactive_ui.protocol import ParsedUi, parse_interactive_ui
from plugins.platforms.matrix.interactive_ui.submission import SubmitSource, submit_sources, validate_action_values
from tests.plugins.platforms.matrix.interactive_ui.test_protocol_conformance import VENDOR, basic, load


def parsed(raw, *, strict=True):
    result = parse_interactive_ui(raw, strict=strict)
    assert isinstance(result, ParsedUi), result
    return result


def fixture(name):
    return parsed(load(VENDOR.joinpath(f"fixtures/valid/{name}.json")))


def button(node_id="send", action_id="send", include=None, **kwargs):
    action = {"type": "submit", "action_id": action_id, **kwargs}
    if include is not None:
        action["include"] = include
    return {"id": node_id, "type": "button", "label": "Send", "action": action}


def stack(children):
    return {"id": "root", "type": "stack", "direction": "vertical", "children": children}


def test_form_keys_label_and_single_use_defaults():
    ui = fixture("form")
    assert submit_sources(ui) == {"answer": SubmitSource("form", None, "answer", frozenset({"name", "quantity", "plan"}), True, "Submit")}
    source = submit_sources(ui)["answer"]
    assert validate_action_values(ui, source, {"name": "Alice", "quantity": 2, "plan": "plus"}) is None
    raw = deepcopy(ui.payload)
    raw["nodes"][0]["single_use"] = False
    assert submit_sources(parsed(raw))["answer"].single_use is False


@pytest.mark.parametrize("single_use", [None, False, True])
def test_button_defaults_and_included_keys(single_use):
    node = button("root", include=["name"], **({} if single_use is None else {"single_use": single_use}))
    ui = parsed(basic([node], {"name": "Alice", "private": "not included"}))
    assert submit_sources(ui) == {"send": SubmitSource("root", None, "send", frozenset({"name"}), single_use is True, "Send")}
    assert validate_action_values(ui, submit_sources(ui)["send"], {"name": "Bob"}) is None


@pytest.mark.parametrize("values,invalid", [
    ({"name": "Alice", "quantity": 1}, ["plan"]),
    ({"name": "Alice", "quantity": 1, "plan": "basic", "extra": "private"}, ["extra"]),
    ({"quantity": 1, "extra": "private"}, ["extra", "name", "plan"]),
    ({"name": "Alice", "quantity": True, "plan": "basic"}, ["quantity"]),
    ({"name": "Alice", "quantity": "1", "plan": "basic"}, ["quantity"]),
    ({"name": "Alice", "quantity": 100, "plan": "removed"}, ["plan", "quantity"]),
    ({"name": "x" * 101, "quantity": float("inf"), "plan": "basic"}, ["name", "quantity"]),
    ({"name": None, "quantity": 1, "plan": "basic"}, ["name"]),
    ({"name": "\ufeff\u00a0 \t\n", "quantity": 1, "plan": "basic"}, ["name"]),
    ({"name": "  Alice  ", "quantity": None, "plan": None}, None),
    ([], ["name", "plan", "quantity"]),
    (None, ["name", "plan", "quantity"]),
])
def test_exact_keys_types_options_and_required_text(values, invalid):
    ui = fixture("form")
    before = deepcopy(ui)
    original = deepcopy(values)
    assert validate_action_values(ui, submit_sources(ui)["answer"], values) == invalid
    assert ui == before
    assert values == original


def test_required_uses_js_trim_and_raw_length():
    ui = fixture("form")
    source = submit_sources(ui)["answer"]
    assert validate_action_values(ui, source, {"name": "\x85", "quantity": 1, "plan": "basic"}) is None
    assert validate_action_values(ui, source, {"name": " " * 100 + "A", "quantity": 1, "plan": "basic"}) == ["name"]


@pytest.mark.parametrize("values,invalid", [({}, None), ({"extra": 1}, ["extra"]), ([], []), (None, []), ({1: "x"}, [])])
def test_no_include_means_exactly_empty_values(values, invalid):
    ui = parsed(basic([button("root")]))
    assert validate_action_values(ui, submit_sources(ui)["send"], values) == invalid


def test_only_reachable_submit_sources_and_no_non_submit_intents():
    raw = basic([stack(["send", "toggle", "link"]), button(), button("unused", "unused"),
                 {"id": "toggle", "type": "button", "label": "Toggle", "action": {"type": "toggle_state", "key": "enabled"}},
                 {"id": "link", "type": "button", "label": "Link", "action": {"type": "open_url", "url": "https://example.org"}}],
                {"enabled": False})
    ui = parsed(raw, strict=False)
    assert set(submit_sources(ui)) == {"send"}


def test_duplicate_action_id_rejected_even_for_client_mode_payload():
    ui = parsed(basic([stack(["a", "b"]), button("a"), button("b")]), strict=False)
    with pytest.raises(ValueError, match="exactly one source"):
        submit_sources(ui)


def test_carousel_item_identity_labels_defaults_and_validation():
    raw = load(VENDOR.joinpath("fixtures/valid/product-carousel.json"))
    raw["state"] = {"selected": "item1"}
    items = raw["nodes"][1]["items"]
    items[0]["action"] = {"type": "submit", "action_id": "choose", "include": ["selected"]}
    items[1]["action"] = {"type": "submit", "action_id": "confirm", "single_use": True}
    del items[2]["action"]
    ui = parsed(raw)
    sources = submit_sources(ui)
    assert sources == {
        "choose": SubmitSource("carousel", "item1", "choose", frozenset({"selected"}), False, "Product 1"),
        "confirm": SubmitSource("carousel", "item2", "confirm", frozenset(), True, "Product 2"),
    }
    assert validate_action_values(ui, sources["choose"], {"selected": "item1"}) is None
    assert validate_action_values(ui, sources["choose"], {"selected": 42}) == ["selected"]
    assert validate_action_values(ui, sources["confirm"], {}) is None
    assert validate_action_values(ui, sources["confirm"], {"selected": "item1"}) == ["selected"]


def test_form_descendants_tabs_collapsible_hidden_required_and_graph_exclusion():
    nodes = [
        {"id": "root", "type": "form", "children": ["tabs", "graph"], "submit_label": "Save", "action_id": "save"},
        {"id": "tabs", "type": "tabs", "bind": "tab", "tabs": [{"value": "a", "label": "A", "child": "details"}]},
        {"id": "details", "type": "collapsible", "label": "Details", "bind": "open", "children": ["name"]},
        {"id": "name", "type": "text_input", "label": "Name", "bind": "name", "required": True, "max_length": 20,
         "visible_when": {"state": "open", "equals": True}},
        {"id": "graph", "type": "graph", "label": "Graph", "bind_selected": "focus",
         "layers": [{"label": "Layer"}], "nodes": [{"id": "a", "label": "A", "layer": 0}], "edges": []},
    ]
    ui = parsed(basic(nodes, {"tab": "a", "open": False, "name": "", "focus": "a"}))
    source = submit_sources(ui)["save"]
    assert source.keys == frozenset({"tab", "open", "name"})
    assert validate_action_values(ui, source, {"tab": "a", "open": False, "name": ""}) == ["name"]
    assert validate_action_values(ui, source, {"tab": "a", "open": False, "name": "Alice"}) is None
    assert validate_action_values(ui, source, {"tab": "a", "open": False, "name": "Alice", "focus": "a"}) == ["focus"]


def test_graph_selection_uses_union_of_bound_graph_nodes():
    graphs = [{"id": node_id, "type": "graph", "label": node_id, "bind_selected": "focus",
               "layers": [{"label": "Layer"}], "nodes": [{"id": value, "label": value, "layer": 0}], "edges": []} for node_id, value in [("g1", "a"), ("g2", "b")]]
    ui = parsed(basic([stack(["g1", "g2", "send"]), *graphs, button(include=["focus"])], {"focus": "a"}))
    source = submit_sources(ui)["send"]
    for value in ("a", "b", None):
        assert validate_action_values(ui, source, {"focus": value}) is None
    for value in ("missing", 42, ["a"]):
        assert validate_action_values(ui, source, {"focus": value}) == ["focus"]


@pytest.mark.parametrize("value,invalid", [(["a"], None), (["a", "b"], None), ([], None),
                                         (["unknown"], ["choices"]), (["a", "a"], ["choices"]),
                                         ("a", ["choices"]), (None, ["choices"]), ([True], ["choices"])])
def test_multiple_choice_options_types_and_uniqueness(value, invalid):
    ui = parsed(basic([stack(["choice", "send"]), button(include=["choices"]),
                       {"id": "choice", "type": "choice", "label": "Choices", "bind": "choices", "multiple": True,
                        "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]}], {"choices": []}))
    assert validate_action_values(ui, submit_sources(ui)["send"], {"choices": value}) == invalid


def test_required_binding_outside_source_and_status_do_not_bypass_durable_dedupe():
    ui = fixture("form")
    raw = deepcopy(ui.payload)
    raw["nodes"].insert(0, stack(["form", "send"]))
    raw["nodes"].append(button(include=["name"]))
    raw["root"] = "root"
    raw["status"] = "submitted"
    ui = parsed(raw)
    source = submit_sources(ui)["send"]
    # Status belongs to atomic accept_action; required checks span all reachable bindings.
    assert validate_action_values(ui, source, {"name": ""}) == ["name"]
    assert validate_action_values(ui, source, {"name": "Alice"}) is None
