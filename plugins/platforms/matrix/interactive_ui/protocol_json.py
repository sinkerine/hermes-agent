"""Bounded JSON copying and ECMAScript scalar semantics for the protocol port.

Copyright 2026 sinkerine. SPDX-License-Identifier: MIT
Port source SPDX: AGPL-3.0-only OR LicenseRef-Element-Commercial.
Additional MIT grant and source commit: vendor/element-web-bcb0801/README.md.
"""

import json
import math
import re

# ECMAScript WhiteSpace + LineTerminator, deliberately excluding U+0085.
JS_WHITESPACE = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
FORBIDDEN_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f]")


class ValidationFailure(Exception):
    def __init__(self, path, code, hint=""):
        self.path, self.code, self.hint = path, code, hint
        super().__init__(path, code)


def fail(path, code, hint=""):
    raise ValidationFailure(path, code, hint)


def field(path, key):
    return f"{path}.{key}" if path else key


def trim(value):
    return value.strip(JS_WHITESPACE)


def kind(value):
    if value is None:
        return "null"
    return {str: "string", bool: "boolean", int: "number", float: "number", list: "array", dict: "object"}.get(type(value), "undefined")


def same(left, right):
    return kind(left) == kind(right) and left == right


def keys(value):
    """Object.keys orders canonical array-index names before other own keys."""
    numeric, other = [], []
    for key in value:
        if type(key) is not str:
            fail("", "bad_type", "JSON object keys must be strings")
        if key.isascii() and key.isdecimal() and len(key) <= 10 and str(int(key)) == key and int(key) < 4294967295:
            numeric.append(key)
        else:
            other.append(key)
    return sorted(numeric, key=int) + other


def js_number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    if number == 0:
        return "0"
    text = repr(number).lower()
    if "e" not in text:
        return text.removesuffix(".0")
    mantissa, exponent = text.split("e")
    power = int(exponent)
    digits = mantissa.removesuffix(".0")
    if -6 <= power < 21:
        sign = "-" if digits.startswith("-") else ""
        digits = digits.lstrip("-")
        whole, _, fraction = digits.partition(".")
        flat = whole + fraction
        point = len(whole) + power
        if point <= 0:
            return sign + "0." + "0" * -point + flat
        if point >= len(flat):
            return sign + flat + "0" * (point - len(flat))
        return sign + flat[:point] + "." + flat[point:]
    return digits + "e" + ("+" if power >= 0 else "-") + str(abs(power))


def js_string(value):
    # JSON.stringify escapes lone UTF-16 surrogates; TextEncoder never sees them.
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return "".join(f"\\u{ord(c):04x}" if 0xD800 <= ord(c) <= 0xDFFF else c for c in text)


def js_json(value):
    value_kind = kind(value)
    if value_kind == "string":
        return js_string(value)
    if value_kind == "number":
        return js_number(value)
    if value_kind == "array":
        return "[" + ",".join(js_json(v) for v in value) + "]"
    if value_kind == "object":
        return "{" + ",".join(js_string(k) + ":" + js_json(value[k]) for k in keys(value)) + "}"
    return {"null": "null", "boolean": "true" if value else "false"}[value_kind]


def copy_json(raw):
    ancestors = set()
    visits = 0
    size = 0

    def count(amount):
        nonlocal size
        size += amount
        if size > 60_000:
            fail("", "too_large", "Client JSON limit is 60000 UTF-8 bytes, including ignored properties")

    def copy(value, path, depth):
        nonlocal visits
        visits += 1
        if visits > 60_000 or depth > 64:
            fail(path, "too_large")
        value_kind = kind(value)
        if value_kind in ("null", "boolean", "string", "number"):
            if value_kind == "string" and len(value.encode("utf-16-le", errors="surrogatepass")) // 2 > 60_000:
                fail("", "too_large")
            if value_kind == "number":
                try:
                    text = js_number(value)
                except (ValueError, OverflowError):
                    fail(path, "bad_value", "JSON numbers must be finite")
            else:
                text = js_json(value)
            count(len(text.encode("utf-8")))
            return float(value) if type(value) is int and abs(value) > 9007199254740991 else value
        if value_kind not in ("array", "object"):
            fail(path, "bad_type")
        if id(value) in ancestors:
            fail(path, "cycle")
        ancestors.add(id(value))
        entries = list(range(len(value))) if value_kind == "array" else keys(value)
        if len(entries) > 30_000:
            fail(path, "too_large")
        out = [] if value_kind == "array" else {}
        count(2)
        for index, key in enumerate(entries):
            count(1 if index else 0)
            child_path = f"{path}[{index}]" if value_kind == "array" else field(path, key)
            if value_kind == "object":
                if len(key.encode("utf-16-le", errors="surrogatepass")) // 2 > 60_000:
                    fail("", "too_large")
                count(len(js_string(key).encode("utf-8")) + 1)
            child = copy(value[key], child_path, depth + 1)
            if value_kind == "array":
                out.append(child)
            else:
                out[key] = child
        ancestors.remove(id(value))
        return out

    return copy(raw, "", 0)
