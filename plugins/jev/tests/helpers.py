"""Shared test fixtures: an isolated JEV_HOME and a fake System One endpoint."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))

KEYS = ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "JEV_API_URL", "PLUGIN_ROOT", "CLAUDE_CONFIG_DIR", "CODEX_HOME")


class JevTestCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._saved_env = dict(os.environ)
        os.environ["JEV_HOME"] = self.home
        os.environ["JEV_PLATFORM"] = "codex"
        for key in KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(self.home, ignore_errors=True)


class FakeJev:
    """Local stand-in for the System One endpoint.

    responder(body) returns the answers map, or None to reply with HTTP 500.
    """

    def __init__(self, responder):
        self.responder = responder
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"body": body, "auth": self.headers.get("Authorization")})
                answers = outer.responder(body)
                if answers is None:
                    self.send_response(500)
                    self.end_headers()
                    return
                data = json.dumps({"model": "jev-test", "answers": answers}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:%d/" % self.server.server_port

    def __enter__(self):
        self.thread.start()
        os.environ["JEV_API_URL"] = self.url
        os.environ["TYPESAFE_API_KEY"] = "test-key"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        os.environ.pop("JEV_API_URL", None)
        os.environ.pop("TYPESAFE_API_KEY", None)


def noul_all(p):
    return lambda body: {q: {"noul": p} for q in body["questions"]}
