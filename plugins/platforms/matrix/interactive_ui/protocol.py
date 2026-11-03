"""Python port of Element Web's v1 interactive UI parser, with producer checks.

Copyright 2026 sinkerine. SPDX-License-Identifier: MIT
Source: apps/web/src/interactive-ui/protocol.ts at
bcb080171cb312feef37e63007c7ca8cc0a8b77f.
Original SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Element-Commercial.
The copyright holder additionally licenses this Python port under MIT;
see vendor/element-web-bcb0801/README.md for the grant and full license.

URL validation follows WHATWG special-URL and UTS46 rules conservatively:
Python/idna Unicode bidi tables can reject obscure hosts accepted by newer ICU
(e.g. U+0879 followed by Myanmar/Chinese letters). Both modes retain this
restriction; canonical fixtures and their first errors are unaffected.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Any, Literal

ErrorCode = Literal[
    "not_object", "bad_version", "too_large", "missing_field", "bad_type", "bad_id",
    "duplicate_id", "unknown_ref", "cycle", "too_deep", "too_many", "too_long",
    "bad_value", "bad_url", "type_mismatch",
]


@dataclass(frozen=True)
class InteractiveUiError:
    path: str
    code: ErrorCode
    hint: str = ""


@dataclass(frozen=True)
class ParsedUi:
    payload: dict[str, Any]
    nodes_by_id: Mapping[str, dict[str, Any]]


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def child_ids(node: Mapping[str, Any]) -> list[str]:
    from .protocol_state import child_ids as get_child_ids

    return get_child_ids(node)


def state_kind(payload: Mapping[str, Any], key: str) -> str:
    from .protocol_state import state_kind as get_state_kind

    return get_state_kind(payload, key)


def accepts_state_value(payload: Mapping[str, Any], key: str, value: object) -> bool:
    from .protocol_state import accepts_state_value as accepts

    return accepts(payload, key, value)


def _validate_strict(payload, nodes_by_id, raw):
    from . import PRODUCER_MAX_PAYLOAD_BYTES
    from .protocol_json import fail

    if len(canonical_json_bytes(raw)) > PRODUCER_MAX_PAYLOAD_BYTES:
        fail("", "too_large", "Producer JSON limit is 40000 UTF-8 bytes, including ignored properties")
    action_ids = set()
    for i, node in enumerate(payload["nodes"]):
        path = f"nodes[{i}]"
        if node["id"] not in nodes_by_id:
            fail(path, "unknown_ref", f"Node {node['id']} is unreachable from root")
        if node["type"] == "unknown":
            fail(f"{path}.type", "bad_type", "Use a supported v1 node type")
        sources = []
        if node["type"] == "form":
            sources.append((node["action_id"], f"{path}.action_id"))
        if node["type"] == "button" and node["action"]["type"] == "submit":
            sources.append((node["action"]["action_id"], f"{path}.action.action_id"))
        if node["type"] == "carousel":
            sources.extend((item["action"]["action_id"], f"{path}.items[{j}].action.action_id")
                           for j, item in enumerate(node["items"])
                           if item.get("action", {}).get("type") == "submit")
        for action_id, action_path in sources:
            if action_id in action_ids:
                fail(action_path, "duplicate_id", f"Submit action_id {action_id} must identify exactly one source")
            action_ids.add(action_id)


def parse_interactive_ui(raw: object, *, strict: bool = False) -> ParsedUi | InteractiveUiError:
    from .protocol_json import ValidationFailure, copy_json, fail, same
    from .protocol_relations import validate_relations
    from .protocol_schema import DEFINITIONS, SCHEMA, read

    try:
        copied = copy_json(raw)
        if type(copied) is not dict:
            fail("", "not_object")
        if not same(copied.get("version"), 1):
            fail("version", "bad_version", "version must be 1")
        payload = read(copied, SCHEMA, "")
        known = {rule["$ref"].split("/")[-1] for rule in DEFINITIONS["node"]["anyOf"][:-1]}
        payload["nodes"] = [node if node["type"] in known else {
            "id": node["id"], "type": "unknown",
            **{key: node[key] for key in ("fallback", "visible_when") if key in node},
        } for node in payload["nodes"]]
        nodes_by_id = validate_relations(payload)
        if strict:
            _validate_strict(payload, nodes_by_id, copied)
        payload["nodes"] = [node for node in payload["nodes"] if node["id"] in nodes_by_id]
        return ParsedUi(payload, nodes_by_id)
    except ValidationFailure as error:
        return InteractiveUiError(error.path, error.code, error.hint)
    except (UnicodeEncodeError, ValueError, OverflowError, RecursionError):
        return InteractiveUiError("", "bad_value", "Payload must be representable as finite UTF-8 JSON")
