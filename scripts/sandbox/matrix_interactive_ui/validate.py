"""Offline harness behavior checks; no services, credentials or live requests."""

import json
import sys

from common import ACTION_KEY, ROOT, SB, entrypoint
from fake_openai import completion
from fixtures import FIXTURES, fixture, validation_payload

sys.path.insert(0, str(ROOT))
from plugins.platforms.matrix.interactive_ui.protocol import InteractiveUiError, parse_interactive_ui
from plugins.platforms.matrix.interactive_ui.tool import PRESENT_UI_SCHEMA


def main():
    assert PRESENT_UI_SCHEMA["name"] == "present_ui"
    for case in FIXTURES:
        full, _ = validation_payload(fixture(case))
        parsed = parse_interactive_ui(full, strict=True)
        assert not isinstance(parsed, InteractiveUiError), f"{case}: {parsed}"
        messages = [{"role": "user", "content": f"[case:{case}]"}]
        first, reason, _ = completion({"messages": messages})
        assert reason == "tool_calls"
        call = first["tool_calls"][0]
        bad, _ = validation_payload(json.loads(call["function"]["arguments"])["ui"])
        assert isinstance(parse_interactive_ui(bad, strict=True), InteractiveUiError)
        messages.extend([first, {"role": "tool", "tool_call_id": call["id"], "content": '{"error":"unknown_ref"}'}])
        corrected, _, _ = completion({"messages": messages})
        good, _ = validation_payload(json.loads(corrected["tool_calls"][0]["function"]["arguments"])["ui"])
        assert not isinstance(parse_interactive_ui(good, strict=True), InteractiveUiError)
    for case in ("fact", "joke"):
        message, reason, _ = completion({"messages": [{"role": "user", "content": f"[case:{case}]"}]})
        assert reason == "stop" and message["content"] and "tool_calls" not in message
    message, reason, _ = completion({"messages": [{"role": "user", "content":
        '[Interactive UI action] values:\n- "focus" = "maillard"\nTo update this card'}]})
    assert reason == "stop" and "maillard" in message["content"]
    assert ACTION_KEY.endswith(".action") and SB.is_relative_to(ROOT)
    print("Offline: 7 strict fixtures, 7 invalid/corrected tool-call pairs, 2 text-only cases, action continuation passed.")
    from validate_review import main as validate_review
    validate_review()


if __name__ == "__main__":
    entrypoint(main)
