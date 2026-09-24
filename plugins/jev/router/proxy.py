#!/usr/bin/env python3
"""jev Codex router: a localhost proxy between Codex and OpenAI that applies Jev's per-turn model choice.

Codex reaches it through `openai_base_url` (set by scripts/codex_router.py). Everything passes through unchanged
except main-agent turn requests with a stored decision; responses stream back byte for byte.
"""
import argparse
import http.client
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("JEV_PLATFORM", "codex")

from jev import config, proxy_rules, state  # noqa: E402

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length"}
RETRY_STATUS = {400, 404, 422}
UPSTREAMS = (("/backend-api/", "JEV_UPSTREAM_CHATGPT", "https://chatgpt.com"),
             ("/v1/", "JEV_UPSTREAM_OPENAI", "https://api.openai.com"))
MEMORY = proxy_rules.Memory()


def upstream_for(path):
    for prefix, variable, default in UPSTREAMS:
        if path.startswith(prefix):
            return os.environ.get(variable) or default
    return None


def decision_for(session, turn):
    settings = config.load_settings()
    if not settings["enabled"] or settings["route"] != "on":
        return None
    return (state.read(session).get("codex_routes") or {}).get(turn)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def handle_one_request(self):
        # Codex dropping a connection is ordinary; without this every disconnect prints a traceback
        # to the agent's log and buries the errors that matter.
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, BrokenPipeError, TimeoutError, OSError):
            self.close_connection = True

    def do_GET(self):
        self.forward()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_GET

    def reply(self, status, data):
        self.send_response_only(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            data = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def forward(self):
        if self.path == "/_jev/health":
            return self.reply(200, b"ok")
        if self.headers.get("Upgrade", "").lower() == "websocket":
            return self.reply(426, b"jev router: use HTTP, not WebSocket")
        base = upstream_for(self.path)
        if base is None:
            return self.reply(404, b"jev router: unknown path")
        body = self.read_body() if self.command in ("POST", "PUT", "PATCH") else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        attempts, plan = [("original", body)], None
        if (self.command == "POST" and self.path.split("?")[0].endswith("/responses")
                and not self.headers.get("Content-Encoding")):
            plan = proxy_rules.plan(body, {k.lower(): v for k, v in self.headers.items()}, decision_for, MEMORY)
            attempts = plan.attempts
            headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}  # readable SSE usage
        for index, (label, payload) in enumerate(attempts):
            try:
                connection, response = self.send_upstream(base, headers, payload)
            except OSError as exc:
                return self.reply(502, ("jev router: upstream unreachable (%s)" % exc).encode())
            if response.status in RETRY_STATUS and index < len(attempts) - 1:
                response.read()
                connection.close()
                continue
            usage = self.relay(response)
            connection.close()
            if plan is not None:
                proxy_rules.remember(MEMORY, plan, label)
                if plan.session:
                    self.log_request(plan, label, response.status, usage)
            self.end_chunks()
            return

    def send_upstream(self, base, headers, payload):
        url = urlsplit(base)
        kind = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        connection = kind(url.hostname, url.port, timeout=900)
        outgoing = dict(headers)
        if payload is not None:
            outgoing["Content-Length"] = str(len(payload))
        connection.request(self.command, self.path, body=payload, headers=outgoing)
        return connection, connection.getresponse()

    def relay(self, response):
        self.send_response_only(response.status, response.reason)
        for key, value in response.getheaders():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)
        self.chunked = not (self.command == "HEAD" or response.status in (204, 304) or response.status < 200)
        if not self.chunked:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        tail = b""
        try:
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
                tail = (tail + chunk)[-262144:]
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection, self.chunked = True, False
        return proxy_rules.usage_from_sse(tail)

    def end_chunks(self):
        """Finish the chunked body (after logging, so the log is written before the client sees the end)."""
        if not self.chunked:
            return
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def log_request(self, plan, label, status, usage):
        try:
            state.log(dict({"kind": "proxy", "session": plan.session, "turn": plan.turn, "request_kind": plan.kind,
                            "requested": plan.requested, "routed": plan.routed and label != "original",
                            "model": plan.requested if label == "original" else plan.model,
                            "effort": plan.effort if label != "original" else None, "via": plan.via,
                            "attempt": label, "status": status}, **(usage or {})))
        except OSError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=config.load_settings()["router_port"])
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
