"""Bounded real-schema model quality receipts; this does not establish browser or Matrix E2E."""

import argparse
from http.client import HTTPException
import json
from pathlib import Path
import sys
import time


from common import ROOT, SB, entrypoint, read_json, request, write_json
import hermes_yaml as yaml
from fixtures import fixture, validation_payload

# Direct script execution imports the current worktree, never a sealed/stale package.
sys.path.insert(0, str(ROOT))
from plugins.platforms.matrix.interactive_ui.protocol import InteractiveUiError, parse_interactive_ui
from plugins.platforms.matrix.interactive_ui.tool import PRESENT_UI_SCHEMA


def evaluate(case):
    messages = [{"role": "system", "content": PRESENT_UI_SCHEMA["description"] + " Sandbox image files: " +
                 ", ".join(str(SB / f"assets/sample-{i}.png") for i in range(3))},
                {"role": "user", "content": case["prompt"]}]
    row = {"id": case["id"], "ui_useful": case["ui_useful"], "requests": 0, "tool_calls": 0,
           "validation_errors": [], "valid_cards": 0, "media_unverified": False, "retry_count": 0,
           "status": "completed"}
    for attempt in range(3):
        row["requests"] += 1
        try:
            result = request("http://127.0.0.1:18082/v1/chat/completions", {
                "model": "gpt-6.1-sol", "stream": False, "messages": messages,
                "tools": [{"type": "function", "function": PRESENT_UI_SCHEMA}], "max_tokens": 4500,
            }, timeout=105)
        except (OSError, ValueError, HTTPException) as exc:
            row.update(status="failed", failure={"kind": "transport_error", "exception": type(exc).__name__,
                       "budget_may_be_spent": True, "ledger": "evidence/live-budget.json"})
            break
        message = result["choices"][0]["message"]
        messages.append(message)
        calls = message.get("tool_calls", [])
        if not calls:
            row["text_reply"] = bool(message.get("content"))
            break
        row["tool_calls"] += len(calls)
        row["retry_count"] = attempt
        for call in calls:
            feedback = validate_call(call, row)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(feedback)})
    row["used_appropriately"] = (row["valid_cards"] > 0) if case["ui_useful"] else row["tool_calls"] == 0
    row["self_corrected"] = bool(row["validation_errors"] and row["valid_cards"])
    return row


def validate_call(call, row):
    try:
        assert call["function"]["name"] == "present_ui"
        args = json.loads(call["function"]["arguments"])
        full, media = validation_payload(args["ui"])
        parsed = parse_interactive_ui(full, strict=True)
        if isinstance(parsed, InteractiveUiError):
            error = f"interactive_ui: {parsed.path}: {parsed.code}: {parsed.hint}"
            row["validation_errors"].append(error)
            return {"error": error}
        row["valid_cards"] += 1
        row["media_unverified"] |= media
        return {"ok": True, "ui_id": "sandbox_eval", "revision": 0,
                "note": "Protocol validation only; no delivery or image upload attempted"}
    except (ValueError, KeyError, TypeError, AssertionError):
        row["validation_errors"].append("malformed_tool_call")
        return {"error": "malformed_tool_call"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("deterministic", "live"), required=True)
    args = parser.parse_args()
    receipt = SB / "run/provider.json"
    assert receipt.exists(), "Start owned provider with up.sh first"
    assert read_json(SB / "run/provider-mode.json")["mode"] == args.mode, "Provider mode must match receipt"
    fixture("carousel")
    cases = yaml.safe_load(Path(__file__).with_name("eval_prompts.yaml").read_text(encoding="utf-8-sig"))
    assert len(cases) * 3 <= 30
    report = {"mode": args.mode, "started_at": time.time(), "scope": "protocol quality only; E2E unverified", "cases": []}
    output = SB / "evidence" / (args.mode + "-eval.json")
    for case in cases:
        report["cases"].append(evaluate(case))
        write_json(output, report)
    report["failed_cases"] = [row["id"] for row in report["cases"] if row["status"] == "failed"]
    report["forced_invalid_self_corrected"] = next(row["self_corrected"] for row in report["cases"] if row["id"] == "invalid")
    write_json(output, report)
    assert report["forced_invalid_self_corrected"], "Forced invalid did not self-correct"
    print(f"Saved {args.mode} quality receipt; browser/Matrix delivery remains separately assessed.")


if __name__ == "__main__":
    entrypoint(main)
