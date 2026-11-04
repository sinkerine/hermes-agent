"""Offline harness behavior checks; no services, credentials or live requests."""

from copy import deepcopy
import json
import sys
from unittest.mock import patch

from common import ACTION_KEY, ROOT, SB, entrypoint
import evaluate
from fake_openai import completion, request_surface
from fixtures import FIXTURES, fixture, validation_payload

sys.path.insert(0, str(ROOT))
from jsonschema import validate
from plugins.platforms.matrix.interactive_ui.protocol import InteractiveUiError, parse_interactive_ui
from plugins.platforms.matrix.interactive_ui.tool import PRESENT_UI_SCHEMA
from tools import registry as registry_module
from tools import tool_search

DIRECT_TOOLS = [{"type": "function", "function": PRESENT_UI_SCHEMA}]


def protocol_result(args):
    full, _ = validation_payload(args["ui"])
    parsed = parse_interactive_ui(full, strict=True)
    if isinstance(parsed, InteractiveUiError):
        assert parsed.path == "root" and parsed.code == "unknown_ref"
        return json.dumps({"error": f"interactive_ui: {parsed.path}: {parsed.code}: {parsed.hint}"})
    return json.dumps({"ok": True, "ui_id": "sandbox_offline", "revision": 0})


def dispatch(call, tools):
    function = call["function"]
    args = json.loads(function["arguments"])
    schema = next(tool["function"] for tool in tools if tool["function"]["name"] == function["name"])
    validate(args, schema["parameters"])
    if function["name"] == "tool_search":
        return tool_search.dispatch_tool_search(args, current_tool_defs=DIRECT_TOOLS)
    if function["name"] == "tool_describe":
        return tool_search.dispatch_tool_describe(args, current_tool_defs=DIRECT_TOOLS)
    name = function["name"]
    if name == "tool_call":
        name, args, error = tool_search.resolve_underlying_call(args, frozenset({"present_ui"}))
        assert error is None and name in tool_search.scoped_deferrable_names(DIRECT_TOOLS)
    assert name == "present_ui"
    validate(args, PRESENT_UI_SCHEMA["parameters"])
    # The registry handler validates protocol only; no producer, gateway or delivery is invoked.
    return registry_module.registry.dispatch(name, args)


def sequence_check(case, tools, expected):
    messages = [{"role": "user", "content": f"[case:{case}]"}]
    original_tools = deepcopy(tools)
    stages = []
    for stage in expected:
        message, reason, label = completion({"messages": messages, "tools": tools})
        assert reason == "tool_calls" and label == f"{case}:{stage}", (stage, label)
        call = message["tool_calls"][0]
        stages.append(call["function"]["name"])
        result = dispatch(call, tools)
        if stage == "invalid":
            assert "interactive_ui: root: unknown_ref:" in json.loads(result)["error"]
        elif stage == "corrected":
            assert json.loads(result)["ok"] is True
        messages.extend([message, {"role": "tool", "tool_call_id": call["id"], "content": result}])
    message, reason, label = completion({"messages": messages, "tools": tools})
    assert reason == "stop" and label == f"{case}:text" and message["content"] == "The card is above."
    assert stages == (["present_ui"] * 2 if len(expected) == 2 else
                      ["tool_search", "tool_describe", "tool_call", "tool_call"])
    assert tools == original_tools
    # An unmatched tool-role message must not advance discovery or consume correction attempts.
    unrelated = {"role": "tool", "tool_call_id": "unrelated", "content": '{"ok":true}'}
    message, reason, label = completion({"messages": [messages[0], unrelated], "tools": tools})
    assert reason == "tool_calls" and label == f"{case}:{expected[0]}"
    return messages


def failure_checks(tools):
    assert completion({"messages": [{"role": "user", "content": "[case:form]"}]})[2] == "form:unavailable"
    first, _, _ = completion({"messages": [{"role": "user", "content": "[case:form]"}], "tools": tools})
    call = first["tool_calls"][0]
    for error in ("send_failed: transport", "Tool does not exist"):
        messages = [{"role": "user", "content": "[case:form]"}, first,
                    {"role": "tool", "tool_call_id": call["id"], "content": json.dumps({"error": error})}]
        message, reason, label = completion({"messages": messages, "tools": tools})
        assert reason == "stop" and label == "form:failed" and "above" not in message["content"]
    deferred = tool_search.bridge_tool_schemas(1)
    messages = [{"role": "user", "content": "[case:form]"}]
    search, _, _ = completion({"messages": messages, "tools": deferred})
    messages.extend([search, {"role": "tool", "tool_call_id": search["tool_calls"][0]["id"],
                              "content": '{"tools":{}}'}])
    assert completion({"messages": messages, "tools": deferred})[2] == "form:search-failed"
    messages[-1]["content"] = '{"tools":{"present_ui":{}}}'
    describe, _, _ = completion({"messages": messages, "tools": deferred})
    messages.extend([describe, {"role": "tool", "tool_call_id": describe["tool_calls"][0]["id"],
                                "content": '{"tools":{}}'}])
    assert completion({"messages": messages, "tools": deferred})[2] == "form:describe-failed"


def evaluator_provider_check():
    def offline_response(_url, data, **_kwargs):
        message, reason, _ = completion(data)
        return {"choices": [{"message": message, "finish_reason": reason}]}
    with patch.object(evaluate, "request", offline_response):
        for case in ("invalid", "fact", "joke"):
            row = evaluate.evaluate({"id": case, "prompt": f"[case:{case}]", "ui_useful": case == "invalid"})
            assert row["status"] == "completed" and row["used_appropriately"]
            if case == "invalid":
                assert row["requests"] == 3 and row["self_corrected"] and row["valid_cards"] == 1
                assert len(row["validation_errors"]) == 1 and "root: unknown_ref" in row["validation_errors"][0]
            else:
                assert row["requests"] == 1 and row["tool_calls"] == 0


def surface_check():
    sentinel = "private request content must not be logged"
    body = {"messages": [{"role": "user", "content": sentinel}], "api_key": sentinel, "tools": [{
        "type": "function", "function": {"name": "tool_call", "description": sentinel, "parameters": {
            "type": "object", "required": ["calls"], "properties": {"calls": {"type": "array", "items": {
                "type": "object", "properties": {"arguments": {"type": "object", "default": sentinel,
                    "description": sentinel, "examples": [sentinel], "enum": [sentinel]}}}}}}}}]}
    summary = request_surface(body)
    assert sentinel not in json.dumps(summary)
    assert summary[0]["parameters"]["properties"]["calls"]["items"]["properties"]["arguments"] == {"type": "object"}


def provider_checks():
    isolated_registry = registry_module.ToolRegistry()
    isolated_registry.register("present_ui", "interactive_ui", PRESENT_UI_SCHEMA, protocol_result)
    config = tool_search.ToolSearchConfig.from_raw({"enabled": "on"})
    # Keep catalog/config inputs synthetic; real search/describe/resolve/registry code runs offline.
    with patch.object(registry_module, "registry", isolated_registry), \
            patch.object(tool_search, "load_config", return_value=config), \
            patch.object(tool_search, "load_config_readonly", return_value=config), \
            patch.object(tool_search, "_record_search_metric"):
        for case in FIXTURES:
            full, _ = validation_payload(fixture(case))
            assert not isinstance(parse_interactive_ui(full, strict=True), InteractiveUiError)
            sequence_check(case, DIRECT_TOOLS, ("invalid", "corrected"))
            assembly = tool_search.assemble_tool_defs(DIRECT_TOOLS, config=config)
            assert assembly.activated and assembly.deferred_count == 1
            messages = sequence_check(case, assembly.tool_defs, ("search", "describe", "invalid", "corrected"))
        failure_checks(DIRECT_TOOLS)
    for case in ("fact", "joke"):
        message, reason, _ = completion({"messages": [{"role": "user", "content": f"[case:{case}]"}],
                                         "tools": tool_search.bridge_tool_schemas(1)})
        assert reason == "stop" and message["content"] and "tool_calls" not in message
    messages.append({"role": "user", "content":
        '[Interactive UI action] values:\n- "focus" = "maillard"\nTo update this card'})
    message, reason, _ = completion({"messages": messages, "tools": tool_search.bridge_tool_schemas(1)})
    assert reason == "stop" and "maillard" in message["content"] and "tool_calls" not in message
    surface_check()
    evaluator_provider_check()
    print("Offline provider: 7 direct + 7 deferred search/describe/invalid/corrected/text sequences passed "
          "through real catalog, bridge resolver, registry and strict parser; text/action/failure/schema-only checks passed.")


def main():
    assert PRESENT_UI_SCHEMA["name"] == "present_ui"
    provider_checks()
    assert ACTION_KEY.endswith(".action") and SB.is_relative_to(ROOT)
    from validate_review import main as validate_review
    validate_review()
    from validate_quality import main as validate_quality
    validate_quality()


if __name__ == "__main__":
    entrypoint(main)
