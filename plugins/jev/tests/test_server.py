import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from helpers import PLUGIN, JevTestCase

try:
    import mcp  # noqa: F401
    import graphify  # noqa: F401
except ImportError:
    raise unittest.SkipTest("mcp/graphify not installed in this interpreter")

from jev import state
from jevsearch import index


class Client:
    def __init__(self):
        self.proc = subprocess.Popen([str(PLUGIN / "scripts" / "jev-search")], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                     env=dict(os.environ))
        self.next_id = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "0"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        while True:
            reply = json.loads(self.proc.stdout.readline())
            if reply.get("id") == self.next_id:
                return reply

    def call(self, arguments, meta=None):
        params = {"name": "jev_search", "arguments": arguments}
        if meta:
            params["_meta"] = meta
        return self.request("tools/call", params)["result"]["content"][0]["text"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()


class ServerTest(JevTestCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.realpath(tempfile.mkdtemp())
        Path(self.repo, "auth.py").write_text("def check_password(user, password):\n    return True\n")
        venv = Path(os.path.expanduser("~/.codex/jev/venv"))
        os.symlink(str(venv), os.path.join(self.home, "venv"))  # the launcher looks under JEV_HOME
        self.client = Client()

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def test_lists_one_tool(self):
        tools = self.client.request("tools/list", {})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["jev_search"])

    def test_builds_then_searches_with_explicit_root(self):
        first = self.client.call({"query": "password", "root": self.repo})
        self.assertIn("Building the code graph", first)
        for _ in range(120):
            if index.read_meta(self.repo).get("state") == "ready" and not index.is_building(self.repo):
                break
            time.sleep(0.5)
        text = self.client.call({"query": "password", "root": self.repo})
        self.assertIn("Repository: %s" % self.repo, text)
        self.assertIn("auth.py", text)

    def test_root_comes_from_session_metadata(self):
        index.build(self.repo)
        with state.session("sess-9") as s:
            s["cwd"] = self.repo
        text = self.client.call({"query": "password"}, meta={"x-codex-turn-metadata": {"session_id": "sess-9"}})
        self.assertIn("Repository: %s" % self.repo, text)
        text = self.client.call({"query": "password"},
                                meta={"x-codex-turn-metadata": json.dumps({"session_id": "sess-9"})})
        self.assertIn("Repository: %s" % self.repo, text)

    def test_unknown_session_asks_for_root(self):
        self.assertIn("root", self.client.call({"query": "x"}))
