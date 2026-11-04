"""Schema-derived feedback supports correction without changing client error ordering."""

import json

import pytest

from plugins.platforms.matrix.interactive_ui.protocol import ParsedUi, parse_interactive_ui
from tests.plugins.platforms.matrix.interactive_ui.test_protocol_conformance import assert_error, basic


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("field,value,expected,corrected", [
    ("gap", 12, ["none", "sm", "md", "lg"], "md"),
    ("direction", "diagonal", ["vertical", "horizontal"], "vertical"),
])
def test_enum_feedback_and_correction(strict, field, value, expected, corrected):
    root = {"id": "root", "type": "stack", "children": [], "direction": "vertical", field: value}
    raw = basic([root])
    error = assert_error(raw, f"nodes[0].{field}", "bad_value", strict)
    assert error.hint.startswith("Expected one of: ")
    assert json.loads(error.hint.removeprefix("Expected one of: ")) == expected
    root[field] = corrected
    assert isinstance(parse_interactive_ui(raw, strict=strict), ParsedUi)


@pytest.mark.parametrize("strict", [False, True])
def test_type_feedback_and_first_error_are_preserved(strict):
    root = {"id": "root", "type": "stack", "children": "wrong", "direction": "diagonal"}
    raw = basic([root])
    error = assert_error(raw, "nodes[0].children", "bad_type", strict)
    assert error.hint == "Expected type: array"
    root["children"] = []
    assert_error(raw, "nodes[0].direction", "bad_value", strict)
    root["direction"] = "vertical"
    assert isinstance(parse_interactive_ui(raw, strict=strict), ParsedUi)
