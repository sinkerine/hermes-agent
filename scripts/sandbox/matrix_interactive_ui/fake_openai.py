"""OpenAI fixture server or live Magpie proxy (default 30, explicitly bounded at 42)."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
import time
import urllib.error
import urllib.request

from common import SB, entrypoint, read_json, write_json
from fixtures import fixture

MAGPIE = "https://magpie-api.sachi.m.mado.moe/v1"
LOCK = threading.Lock()


def selected_case(text):
    for case in ("graph-zh", "graph-en", "table", "chart", "form", "carousel", "invalid", "fact", "joke"):
        if f"[case:{case}]" in text:
            return case
    if any("\u4e00" <= char <= "\u9fff" for char in text):
        return "graph-zh"
    for keyword, case in (("table", "table"), ("chart", "chart"), ("week", "form"), ("carousel", "carousel")):
        if keyword in text.lower():
            return case
    return "graph-en"


def tool_reply(name, arguments, case):
    message = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_" + secrets.token_hex(8), "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }]}
    return message, "tool_calls", case


def completed_calls(messages):
    """Pair results by ID: catalog lookups must not consume the UI correction attempt."""
    pending = {}
    completed = []
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls", []):
                pending[call["id"]] = call["function"]
        elif message.get("role") == "tool" and message.get("tool_call_id") in pending:
            function = pending.pop(message["tool_call_id"])
            completed.append((function["name"], json.loads(function["arguments"]),
                              json.loads(message["content"])))
    return completed


def schema_shape(schema):
    """Keep structural schema fields only; never descriptions, defaults or example values."""
    shape = {key: schema[key] for key in ("type", "required") if key in schema}
    if "properties" in schema:
        shape["properties"] = {name: schema_shape(value) for name, value in schema["properties"].items()}
    if isinstance(schema.get("items"), dict):
        shape["items"] = schema_shape(schema["items"])
    return shape


def request_surface(body):
    return [{"name": tool["function"]["name"],
             "parameters": schema_shape(tool["function"].get("parameters", {}))}
            for tool in body.get("tools", []) if tool.get("type") == "function"]


def card_completion(body, trailing, case):
    names = {tool["function"]["name"] for tool in body.get("tools", []) if tool.get("type") == "function"}
    completed = completed_calls(trailing)
    direct = "present_ui" in names
    if not direct:
        if not {"tool_search", "tool_describe", "tool_call"}.issubset(names):
            return {"role": "assistant", "content": "The card tool is unavailable."}, "stop", case + ":unavailable"
        searches = [result for name, _, result in completed if name == "tool_search"]
        if not searches:
            return tool_reply("tool_search", {"queries": ["present_ui"]}, case + ":search")
        if "present_ui" not in searches[-1].get("tools", {}):
            return {"role": "assistant", "content": "The card tool was not found."}, "stop", case + ":search-failed"
        descriptions = [result for name, _, result in completed if name == "tool_describe"]
        if not descriptions:
            return tool_reply("tool_describe", {"names": ["present_ui"]}, case + ":describe")
        if not descriptions[-1].get("tools", {}).get("present_ui", {}).get("parameters"):
            return {"role": "assistant", "content": "The card schema is unavailable."}, "stop", case + ":describe-failed"
    results = [result for name, args, result in completed if name == "present_ui" or (
        name == "tool_call" and any(call.get("name") == "present_ui" for call in args.get("calls", [])))]
    if results:
        if results[-1].get("ok") is True:
            return {"role": "assistant", "content": "The card is above."}, "stop", case + ":text"
        if len(results) >= 2 or "interactive_ui: root: unknown_ref:" not in str(results[-1].get("error", "")):
            return {"role": "assistant", "content": "Unable to present the card."}, "stop", case + ":failed"
    ui = fixture(case)
    # Every card request exercises the real validator's path-specific error and one correction.
    if not results:
        ui["root"] = "missing_root"
    args = {"ui": ui}
    name = "present_ui" if direct else "tool_call"
    arguments = args if direct else {"calls": [{"name": "present_ui", "arguments": args}]}
    return tool_reply(name, arguments, case + (":corrected" if results else ":invalid"))


def completion(body):
    messages = body.get("messages", [])
    last_user = max((i for i, item in enumerate(messages) if item.get("role") == "user"), default=-1)
    text = str(messages[last_user].get("content", "")) if last_user >= 0 else ""
    trailing = messages[last_user + 1:]
    case = selected_case(text)
    if "[Interactive UI action]" in text:
        # Carry the selected values into the visible answer so the operator can verify routing.
        content = "Selection received. " + text.split("values:", 1)[-1].split("To update", 1)[0].strip()
        return {"role": "assistant", "content": content}, "stop", "action-continuation"
    if case in ("fact", "joke"):
        content = {"fact": "Paris is the capital of France.", "joke": "Why did the byte cross the bus? To get to the other side."}[case]
        return {"role": "assistant", "content": content}, "stop", case
    return card_completion(body, trailing, case)


def load_magpie_key():
    # Only called after --live. Select one provider; never copy or print the config.
    from pathlib import Path
    document = read_json(Path.home() / ".pi/agent/models.json")
    providers = document.get("providers", {})
    candidates = [value for name, value in providers.items() if "magpie" in name.lower()
                  or value.get("baseUrl", "").rstrip("/") == MAGPIE]
    assert len(candidates) == 1, "Expected one explicit Magpie provider"
    provider = candidates[0]
    assert provider.get("baseUrl", "").rstrip("/") == MAGPIE
    key = provider["apiKey"]
    assert isinstance(key, str) and key and "\n" not in key
    return key


def live_request_limit(value):
    limit = int(value)
    if not 1 <= limit <= 42:
        raise argparse.ArgumentTypeError("Live request limit must be between 1 and 42")
    return limit


def configure_live_budget(limit=None):
    """Persist explicit authorization without resetting reservations or prior authorization."""
    assert limit is None or (type(limit) is int and 1 <= limit <= 42)
    path = SB / "evidence/live-budget.json"
    with LOCK:
        ledger = read_json(path) if path.exists() else {"limit": limit or 30, "reserved_requests": 0}
        assert type(ledger["limit"]) is int and 1 <= ledger["limit"] <= 42
        assert type(ledger["reserved_requests"]) is int and ledger["reserved_requests"] >= 0
        if limit is not None:
            ledger["limit"] = max(ledger["limit"], limit)
        write_json(path, ledger)


def reserve_live(limit=30):
    assert type(limit) is int and 1 <= limit <= 42
    path = SB / "evidence/live-budget.json"
    with LOCK:
        ledger = read_json(path)
        if ledger["reserved_requests"] >= min(limit, ledger["limit"]):
            return None
        ledger["reserved_requests"] += 1
        write_json(path, ledger)
        return ledger["reserved_requests"]


def receipt(mode, data):
    path = SB / "evidence" / (mode + "-provider.jsonl")
    with LOCK, path.open("a", encoding="utf-8") as output:
        output.write(json.dumps({"time": time.time(), **data}) + "\n")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Do not log request bodies, URLs with tokens, headers, or model text.
        return

    def send_json(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.send_json(200, {"object": "list", "data": [{"id": "gpt-6.1-sol", "object": "model",
                                                       "context_length": 128000}]})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_json(404, {"error": {"message": "chat/completions only"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 4_000_000:
            self.send_json(413, {"error": {"message": "request size"}})
            return
        body = json.loads(self.rfile.read(length))
        if body.get("stream"):
            self.send_json(400, {"error": {"message": "stream unsupported; disable model.streaming"}})
            return
        if self.server.live:
            self.forward_live(body)
            return
        message, reason, case = completion(body)
        receipt("deterministic", {"case": case, "tool_calls": len(message.get("tool_calls", [])),
                                  "request_tools": request_surface(body)})
        self.send_json(200, {"id": "chatcmpl-" + secrets.token_hex(8), "object": "chat.completion",
                             "created": int(time.time()), "model": body.get("model", "gpt-6.1-sol"),
                             "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                             "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

    def forward_live(self, body):
        index = reserve_live(self.server.live_request_limit)
        if index is None:
            self.send_json(429, {"error": {"message": "Sandbox's persistent live request limit reached"}})
            return
        body["model"] = "gpt-6.1-sol"
        req = urllib.request.Request(MAGPIE + "/chat/completions", data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + self.server.api_key,
        })
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                result = json.load(response)
            receipt("live", {"request": index, "status": "response", "tool_calls": len(
                result.get("choices", [{}])[0].get("message", {}).get("tool_calls", []))})
            self.send_json(200, result)
        except (urllib.error.URLError, TimeoutError):
            receipt("live", {"request": index, "status": "transport_error"})
            self.send_json(502, {"error": {"message": "Magpie transport failed (details withheld)"}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--live-request-limit", type=live_request_limit,
                        help="Authorized total cap across restarts (1..42, default 30)")
    args = parser.parse_args()
    if args.live_request_limit is not None and not args.live:
        parser.error("--live-request-limit requires --live")
    # One listener with SO_REUSEPORT disabled prevents concurrent budget owners.
    server = ThreadingHTTPServer(("127.0.0.1", 18082), Handler)
    server.live = args.live
    server.live_request_limit = args.live_request_limit or 30
    if args.live:
        configure_live_budget(args.live_request_limit)
    server.api_key = load_magpie_key() if args.live else None
    server.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    entrypoint(main)
