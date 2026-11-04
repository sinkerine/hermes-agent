"""Interpreter of the pinned Element Web schema (including its error ordering).

Copyright 2026 sinkerine. SPDX-License-Identifier: MIT
Port source SPDX: AGPL-3.0-only OR LicenseRef-Element-Commercial.
Additional MIT grant and source commit: vendor/element-web-bcb0801/README.md.
"""

import ipaddress
import json
import math
import re

import idna

from importlib.resources import files
from urllib.parse import unquote, urlsplit

from .protocol_json import (
    FORBIDDEN_CONTROLS, ValidationFailure, fail, field, js_json, keys, kind, same, trim,
)

SCHEMA = json.loads(files(__package__).joinpath("vendor/element-web-bcb0801/interactive-ui-v1.schema.json").read_text(encoding="utf-8-sig"))
DEFINITIONS = SCHEMA["$defs"]


def resolve(rule):
    return DEFINITIONS[rule["$ref"].split("/")[-1]] if "$ref" in rule else rule


def valid_ipv4(host):
    parts = host.rstrip(".").split(".")
    if len(parts) > 4:
        return False
    numbers = []
    for part in parts:
        if not part:
            return False
        base = 16 if part.lower().startswith("0x") else 8 if part.startswith("0") and len(part) > 1 else 10
        digits = part[2:] if base == 16 else part[1:] if base == 8 else part
        # WHATWG allows an empty radix suffix, but never signs or Python digit separators.
        pattern = {16: r"[0-9a-fA-F]*", 8: r"[0-7]*", 10: r"[0-9]+"}[base]
        if re.fullmatch(pattern, digits) is None:
            return False
        try:
            numbers.append(int(digits or "0", base))
        except ValueError:
            return False
    return all(0 <= n < 256 for n in numbers[:-1]) and 0 <= numbers[-1] < 256 ** (5 - len(numbers))


def ascii_host(host):
    # WHATWG uses nontransitional UTS46 without STD3, hyphen or DNS length checks.
    mapped = idna.uts46_remap(host, std3_rules=False, transitional=False)
    labels = []
    for label in mapped.split("."):
        if label.isascii():
            labels.append(label)
            continue
        idna.check_initial_combiner(label)
        idna.check_bidi(label)
        for pos, char in enumerate(label):
            if char in ("\u200c", "\u200d") and not idna.valid_contextj(label, pos):
                raise ValueError("invalid contextual joiner")
        labels.append("xn--" + label.encode("punycode").decode("ascii"))
    return ".".join(labels)


def valid_https(value):
    # WHATWG special URLs treat backslashes as separators and allow empty userinfo.
    candidate = value.replace("\t", "").replace("\n", "").replace("\r", "").replace("\\", "/")
    if not candidate.lower().startswith("https:"):
        return False
    candidate = "https://" + candidate[6:].lstrip("/")
    try:
        url = urlsplit(candidate)
        host = unquote(url.hostname or "", errors="strict")
        if not host or url.username or url.password:
            return False
        if url.port is not None and not 0 <= url.port <= 65535:
            return False
        if ":" in host:
            ipaddress.IPv6Address(host)
            return "%" not in host
        if re.search(r"[\x00-\x20\x7f#%/:<>?@\[\\\]^|]", host):
            return False
        host = ascii_host(host)
        if not host or re.search(r"[\x00-\x20\x7f#%/:<>?@\[\\\]^|]", host):
            return False
        last = host.rstrip(".").split(".")[-1]
        if last.isascii() and (last.isdecimal() or re.fullmatch(r"0[xX][0-9a-fA-F]*", last)):
            return valid_ipv4(host)
        return True
    except (ValueError, UnicodeError):
        return False


def matches_type(value, expected):
    if expected == "integer":
        return kind(value) == "number" and math.isfinite(value) and value == int(value) and abs(value) <= 9007199254740991
    return kind(value) == expected


def choose_alternative(value, alternatives):
    if type(value) is not dict:
        return None
    for alternative in alternatives:
        for key, rule in resolve(alternative).get("properties", {}).items():
            candidate = value.get(key)
            if "const" in rule and same(trim(candidate) if type(candidate) is str else candidate, rule["const"]):
                return alternative
    return None


def read_union(value, rule, path):
    alternatives = rule.get("anyOf", rule.get("oneOf"))
    selected = choose_alternative(value, alternatives)
    if selected is not None:
        return read(value, selected, path)
    if type(value) is dict and type(value.get("type")) is str and rule is DEFINITIONS["node"]:
        return read(value, DEFINITIONS["unknown"], path)
    first_error = None
    for alternative in alternatives:
        try:
            return read(value, alternative, path)
        except ValidationFailure as error:
            if first_error is None:
                first_error = error
    if first_error and first_error.code not in ("bad_type", "not_object"):
        raise first_error
    fail(path, "bad_type")


def satisfies(value, rule, path):
    try:
        read(value, rule, path)
        return True
    except ValidationFailure:
        return False


def read_constraints(value, rule, path):
    for constraint in rule.get("allOf", []):
        read(value, constraint, path)
    if "if" in rule:
        branch = rule.get("then" if satisfies(value, rule["if"], path) else "else")
        if branch is not None:
            read(value, branch, path)
    if "not" in rule and satisfies(value, rule["not"], path):
        fail(path, "bad_value")
    if "const" in rule and not same(value, rule["const"]):
        fail(path, rule.get("x-error", "bad_value"), f"Expected value: {js_json(rule['const'])}")
    if "enum" in rule and not any(same(value, option) for option in rule["enum"]):
        fail(path, rule.get("x-error", "bad_value"), f"Expected one of: {js_json(rule['enum'])}")
    if "type" in rule:
        expected = rule["type"] if type(rule["type"]) is list else [rule["type"]]
        if not any(matches_type(value, t) for t in expected):
            fail(path, "not_object" if rule["type"] == "object" else "bad_type",
                 "Expected type: " + " or ".join(expected))


def read_string(value, rule, path):
    if rule is DEFINITIONS["https"] and not valid_https(value):
        fail(path, "bad_url", "Use an HTTPS URL with a host and no credentials")
    if "maxLength" in rule and len(value) > rule["maxLength"]:
        fail(path, "too_long", f"Maximum length is {rule['maxLength']} code points after trimming")
    if "minLength" in rule and len(value) < rule["minLength"]:
        fail(path, rule.get("x-error", "bad_value"))
    # JS \\s uses the ECMAScript trim set, unlike Python's Unicode or ASCII classes.
    from .protocol_json import JS_WHITESPACE

    pattern = rule.get("pattern", "").replace(r"\s", re.escape(JS_WHITESPACE))
    if pattern and not re.search(pattern, value):
        fail(path, rule.get("x-error", "bad_value"), f"Must match {rule['pattern']}")


def read_array(value, rule, path):
    if "maxItems" in rule and len(value) > rule["maxItems"]:
        fail(path, "too_many", f"Maximum item count is {rule['maxItems']}")
    if "minItems" in rule and len(value) < rule["minItems"]:
        fail(path, "bad_value", f"Minimum item count is {rule['minItems']}")
    out = [read(v, rule["items"], f"{path}[{i}]") if "items" in rule else v for i, v in enumerate(value)]
    if rule.get("uniqueItems") and len({js_json(v) for v in out}) != len(out):
        fail(path, "duplicate_id")
    return out


def read_object(value, rule, path):
    for key in rule.get("required", []):
        if key not in value:
            fail(field(path, key), "missing_field", f"Required field: {key}")
    if "maxProperties" in rule and len(value) > rule["maxProperties"]:
        fail(path, "too_many", f"Maximum property count is {rule['maxProperties']}")
    out = {}
    for key in keys(value):
        child_path = field(path, key)
        if "propertyNames" in rule and read(key, rule["propertyNames"], child_path) != key:
            fail(child_path, "bad_id")
        child_rule = rule.get("properties", {}).get(key)
        if child_rule is None and type(rule.get("additionalProperties")) is dict:
            child_rule = rule["additionalProperties"]
        if child_rule is not None:
            out[key] = read(value[key], child_rule, child_path)
        elif rule.get("additionalProperties") is False:
            fail(child_path, "bad_value")
    return out


def read(value, rule, path):
    rule = resolve(rule)
    if type(value) is str:
        if FORBIDDEN_CONTROLS.search(value):
            fail(path, "bad_value", "C0 controls other than tab and newline are forbidden")
        value = trim(value)
    if "anyOf" in rule or "oneOf" in rule:
        return read_union(value, rule, path)
    read_constraints(value, rule, path)
    value_kind = kind(value)
    if value_kind == "string":
        read_string(value, rule, path)
    if value_kind == "number":
        comparisons = {"minimum": lambda n: value < n, "maximum": lambda n: value > n, "exclusiveMinimum": lambda n: value <= n}
        for name, outside in comparisons.items():
            if name in rule and outside(rule[name]):
                fail(path, "bad_value", f"{name} is {rule[name]}")
    if value_kind == "array":
        return read_array(value, rule, path)
    if value_kind == "object":
        return read_object(value, rule, path)
    return value
