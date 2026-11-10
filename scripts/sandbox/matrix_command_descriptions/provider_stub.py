"""Loopback-only recording OpenAI stub; command execution must never call it."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from common import SB, guard, write_json

COUNT = 0
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == '/v1/models':
            self.reply({'object': 'list', 'data': [{'id': 'sandbox', 'object': 'model'}]})
        elif self.path == '/counts':
            with LOCK:
                self.reply({'completions': COUNT})
        else:
            self.reply({'error': 'unknown endpoint'}, 404)

    def do_POST(self):
        global COUNT
        if self.path != '/v1/chat/completions':
            self.reply({'error': 'unknown endpoint'}, 404)
            return
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        with LOCK:
            COUNT += 1
            write_json(SB / 'evidence/provider-counts.json', {'completions': COUNT})
        self.reply({'id': 'sandbox', 'object': 'chat.completion', 'model': 'sandbox',
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Unexpected model request'},
                                 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}})


if __name__ == '__main__':
    guard()
    write_json(SB / 'evidence/provider-counts.json', {'completions': 0})
    ThreadingHTTPServer(('127.0.0.1', 18182), Handler).serve_forever()
