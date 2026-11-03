"""Seeded mutation and non-JSON safety contracts for the parser."""

from copy import deepcopy
from random import Random

import pytest

from plugins.platforms.matrix.interactive_ui.protocol import InteractiveUiError, ParsedUi, parse_interactive_ui
from tests.plugins.platforms.matrix.interactive_ui.test_protocol_conformance import VALID, assert_error, basic, load


def test_seeded_fixture_mutations_never_raise():
    rng = Random(0xBCB0801)
    fixtures = [load(path) for path in VALID]
    replacements = [None, [], {}, False, 1, 1.0, "bad\x00", float("inf"), float("nan"), "x" * 61000]
    for _ in range(2500):
        raw = deepcopy(rng.choice(fixtures))
        containers = [raw]
        for _ in range(rng.randrange(1, 5)):
            current = containers[-1]
            children = list(current.values()) if type(current) is dict else current
            children = [value for value in children if type(value) in (dict, list)]
            if not children:
                break
            containers.append(rng.choice(children))
        target = rng.choice(containers)
        key = (rng.choice(list(target)) if type(target) is dict else rng.randrange(len(target))) if target else None
        if key is not None:
            if rng.randrange(4) == 0:
                del target[key]
            else:
                target[key] = deepcopy(rng.choice(replacements))
        result = parse_interactive_ui(raw)
        strict = parse_interactive_ui(raw, strict=True)
        assert isinstance(result, (ParsedUi, InteractiveUiError))
        assert isinstance(strict, (ParsedUi, InteractiveUiError))
        if isinstance(result, InteractiveUiError):
            assert (strict.path, strict.code) == (result.path, result.code)
        if isinstance(strict, ParsedUi):
            assert strict == result
        if isinstance(result, ParsedUi):
            assert parse_interactive_ui(result.payload) == result


@pytest.mark.parametrize("value,path,code", [(float("nan"), "ignored", "bad_value"), (float("inf"), "ignored", "bad_value"),
                                            (set(), "ignored", "bad_type"), (object(), "ignored", "bad_type"),
                                            (lambda: 1, "ignored", "bad_type")])
def test_ignored_properties_still_require_safe_json(value, path, code):
    assert_error({**basic(), "ignored": value}, path, code)


def test_cycles_are_reported_at_the_reference_path():
    raw = basic()
    raw["self"] = raw
    for strict in (False, True):
        assert_error(raw, "self", "cycle", strict)


def test_shared_non_cyclic_containers_are_copied():
    shared = ["shared"]
    assert isinstance(parse_interactive_ui({**basic(), "a": shared, "b": shared}), ParsedUi)


def test_depth_and_container_limits_are_bounded():
    raw = basic()
    current = raw
    for _ in range(65):
        current["x"] = {}
        current = current["x"]
    result = parse_interactive_ui(raw)
    assert isinstance(result, InteractiveUiError)
    assert result.code == "too_large"
    assert_error({**basic(), "array": [None] * 30_001}, "array", "too_large")
    assert_error({**basic(), "string": "😀" * 30_001}, "", "too_large")


def test_hostile_subclasses_do_not_execute_user_code():
    class Hostile(dict):
        def __iter__(self):
            raise AssertionError("must not execute")

        def __getitem__(self, key):
            raise AssertionError("must not execute")

    assert_error(Hostile(), "", "bad_type")
    assert_error({**basic(), "ignored": Hostile()}, "ignored", "bad_type")
