import http.client
import importlib.util
import json
import os
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import PLUGIN, FakeJev, JevTestCase
from jev import codex_route, config, state

SSE = (b'event: response.created\ndata: {"type":"response.created"}\n\n'
       b'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":1000,'
       b'"input_tokens_details":{"cached_tokens":800},"output_tokens":50}}}\n\n')


def load_proxy():
    spec = importlib.util.spec_from_file_location("jev_router_proxy", str(PLUGIN / "router" / "proxy.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Upstream:
    def __init__(self):
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.seen.append(("GET", self.path, None, dict(self.headers)))
                data = b'{"models": []}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.seen.append(("POST", self.path, body, dict(self.headers)))
                model = json.loads(body).get("model") if body[:1] == b"{" else None
                if model == "bad-model":
                    data = b'{"error": {"message": "unsupported model"}}'
                    self.send_response(400)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for part in (SSE[:40], SSE[40:]):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                self.wfile.write(b"0\r\n\r\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ProxyTest(JevTestCase):
    def setUp(self):
        super().setUp()
        self.upstream = Upstream()
        os.environ["JEV_UPSTREAM_CHATGPT"] = "http://127.0.0.1:%d" % self.upstream.server.server_port
        self.proxy = load_proxy()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.proxy.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.upstream.close()
        super().tearDown()

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def post_turn(self, model="gpt-6-astra", kind="turn", extra_headers=None):
        body = json.dumps({"model": model, "reasoning": {"effort": "xhigh"}, "stream": True, "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]})
        headers = {"Content-Type": "application/json", "Authorization": "Bearer secret",
                   "x-codex-turn-metadata": json.dumps({"session_id": "s1", "turn_id": "t1", "request_kind": kind})}
        headers.update(extra_headers or {})
        return self.call("POST", "/backend-api/codex/responses", body, headers)

    def route(self, model, effort):
        with state.session("s1") as s:
            s["codex_routes"] = {"t1": {"model": model, "effort": effort, "tier": 1}}

    def test_health_websocket_and_passthrough(self):
        self.assertEqual(self.call("GET", "/_jev/health"), (200, b"ok"))
        self.assertEqual(self.call("GET", "/backend-api/codex/responses", headers={"Upgrade": "websocket",
                                                                                   "Connection": "Upgrade"})[0], 426)
        self.assertEqual(self.call("GET", "/backend-api/codex/models"), (200, b'{"models": []}'))
        self.assertEqual(self.upstream.seen[-1][3].get("Authorization"), None)  # none sent in this call

    def test_routed_turn_streams_back_unchanged_and_logs_usage(self):
        self.route("gpt-5.6-luna", "low")
        status, data = self.post_turn()
        self.assertEqual((status, data), (200, SSE))
        sent = json.loads(self.upstream.seen[-1][2])
        self.assertEqual((sent["model"], sent["reasoning"]["effort"]), ("gpt-5.6-luna", "low"))
        self.assertEqual(self.upstream.seen[-1][3]["Authorization"], "Bearer secret")
        event = [e for e in state.events("s1") if e["kind"] == "proxy"][-1]
        self.assertEqual((event["model"], event["requested"], event["input"], event["cached"], event["output"]),
                         ("gpt-5.6-luna", "gpt-6-astra", 1000, 800, 50))

    def test_route_off_and_other_kinds_pass_through(self):
        self.route("gpt-5.6-luna", "low")
        config.save_settings({"route": "shadow"})
        self.post_turn()
        self.assertEqual(json.loads(self.upstream.seen[-1][2])["model"], "gpt-6-astra")
        config.save_settings({"route": "on"})
        self.post_turn(kind="compaction")
        self.assertEqual(json.loads(self.upstream.seen[-1][2])["model"], "gpt-6-astra")

    def test_rejected_route_falls_back_to_requested_model(self):
        self.route("bad-model", "low")
        status, data = self.post_turn()
        self.assertEqual((status, data), (200, SSE))
        models = [json.loads(seen[2])["model"] for seen in self.upstream.seen]
        self.assertEqual(models, ["bad-model", "bad-model", "gpt-6-astra"])
        self.assertEqual([e for e in state.events("s1") if e["kind"] == "proxy"][-1]["attempt"], "original")

    def test_compressed_bodies_pass_through(self):
        self.route("gpt-5.6-luna", "low")
        self.post_turn(extra_headers={"Content-Encoding": "zstd"})
        self.assertEqual(json.loads(self.upstream.seen[-1][2])["model"], "gpt-6-astra")

    def test_a_prompt_hook_decision_reaches_the_wire(self):
        """End to end: the prompt hook stores a tier, and the next turn request is sent on that model."""
        os.environ["CODEX_HOME"] = self.home
        Path(self.home, "config.toml").write_text('openai_base_url = "x"  # jev-router\n')
        transcript = os.path.join(self.home, "t.jsonl")
        Path(transcript).write_text("")
        with FakeJev(lambda body: {"tier": {"choice": "1", "confidence": 0.95}, "followup": {"noul": 0.0}}):
            notice = codex_route.on_prompt({"session_id": "s1", "turn_id": "t1", "model": "gpt-6-astra",
                                            "cwd": "/tmp", "transcript_path": transcript},
                                           config.load_settings(), "what does this repo do?")
        self.assertIn("jev → gpt-6-luna · low (tier 1", notice["systemMessage"])
        self.assertEqual(self.post_turn()[0], 200)
        self.assertEqual(json.loads(self.upstream.seen[-1][2])["model"], "gpt-6-luna")
