"""Threaded loopback Hindsight service for backend and gateway behavior tests."""
from __future__ import annotations

from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import unquote


class HindsightStandin:
    def __init__(self) -> None:
        self.banks: dict[str, list[dict]] = {}
        self._faults: dict[str, deque] = defaultdict(deque)
        self._requests: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner._handle(self)

            def do_PUT(self):
                owner._handle(self)

            def do_POST(self):
                owner._handle(self)

            def log_message(self, format, *args):
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *args):
        self.close()

    def close(self) -> None:
        self._stop.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def seed(self, bank_id: str, results: list[dict]) -> None:
        with self._lock:
            self.banks[bank_id] = list(results)

    def queue_fault(self, bank_id: str, *, status: int = 500, delay: float = 0) -> None:
        with self._lock:
            self._faults[bank_id].append((status, delay))

    def requests(self, path_prefix: str = "") -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._requests if row["path"].startswith(path_prefix)]

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", "0"))
        body = json.loads(handler.rfile.read(length)) if length else None
        parts = handler.path.split("/")
        bank_id = unquote(parts[4]) if len(parts) >= 5 else ""
        with self._lock:
            self._requests.append({"method": handler.command, "path": handler.path,
                                   "headers": dict(handler.headers), "body": body})
            fault = self._faults[bank_id].popleft() if self._faults[bank_id] else None
            status, payload = (fault[0], {}) if fault else self._route(handler.command, parts, bank_id, body)
        if fault:
            status, delay = fault
            self._stop.wait(delay)
            payload = {"error": "queued fault"}
        encoded = json.dumps(payload).encode()
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(encoded)))
            handler.end_headers()
            handler.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            # Timeout tests deliberately close the client before a delayed response.
            return

    def _route(self, method: str, parts: list[str], bank_id: str, body) -> tuple[int, dict]:
        if parts[:4] != ["", "v1", "default", "banks"] or not bank_id:
            return 404, {}
        suffix = parts[5:]
        if method == "PUT" and not suffix:
            self.banks.setdefault(bank_id, [])
            return 200, {"bank_id": bank_id, "name": body["name"]}
        if bank_id not in self.banks:
            return 404, {}
        if method == "GET" and suffix == ["config"]:
            return 200, {"bank_id": bank_id}
        if method == "POST" and suffix == ["memories", "recall"]:
            return 200, {"results": self.banks[bank_id]}
        if method == "POST" and suffix == ["memories"]:
            return 202, {"success": True}
        return 404, {}
