"""Shared role-identifiable HTTP endpoint (standard library only). Used by primary.py and backup.py.

Same file is deployed twice: ROLE=primary on one host, ROLE=backup on another.

Endpoints
  GET  /          HTML status page containing the keyword  ENDPOINT-ROLE:<role>
  GET  /whoami    JSON identity + server time + uptime
  GET  /health    200 "HEALTHY role=<role>"  or  503 "UNHEALTHY role=<role>" when chaos is active
  POST /_chaos/down   make this instance report failure (needs header X-Chaos-Token)
  POST /_chaos/up     recover                           (needs header X-Chaos-Token)

Environment
  PORT         listen port        (default 8080)
  CHAOS_TOKEN  secret enabling the chaos endpoints. If unset they are disabled.
"""
import hmac
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class State:
    def __init__(self, role, token):
        self.role = role
        self.token = token or ""
        self.down = False
        self.down_since = None
        self.started = utcnow()
        self.started_mono = time.monotonic()
        self.lock = threading.Lock()


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        server_version = "failover-demo/1.0"

        def log_message(self, fmt, *args):
            print("%s %s %s" % (utcnow(), self.address_string(), fmt % args), flush=True)

        def _send(self, code, body, ctype="text/plain; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Endpoint-Role", state.role)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _path(self):
            return self.path.split("?", 1)[0]

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            p = self._path()
            if p == "/health":
                if state.down:
                    return self._send(503, "UNHEALTHY role=%s\n" % state.role)
                return self._send(200, "HEALTHY role=%s\n" % state.role)
            if state.down:
                return self._send(503, "ENDPOINT-ROLE:%s state=failed\n" % state.role)
            if p == "/whoami":
                body = json.dumps({
                    "role": state.role,
                    "server_time_utc": utcnow(),
                    "instance_started_utc": state.started,
                    "uptime_s": round(time.monotonic() - state.started_mono, 1),
                    "healthy": True,
                })
                return self._send(200, body + "\n", "application/json")
            if p == "/":
                html = (
                    "<!doctype html><meta charset=utf-8><title>%(r)s</title>"
                    "<body style='font-family:sans-serif;text-align:center;margin-top:12vh'>"
                    "<h1>ENDPOINT-ROLE:%(r)s</h1>"
                    "<p>Server time (UTC): %(t)s</p><p>Instance started: %(s)s</p></body>"
                ) % {"r": state.role, "t": utcnow(), "s": state.started}
                return self._send(200, html, "text/html; charset=utf-8")
            return self._send(404, "not found\n")

        def do_POST(self):
            p = self._path()
            if p not in ("/_chaos/down", "/_chaos/up"):
                return self._send(404, "not found\n")
            if not state.token:
                return self._send(403, "chaos endpoints disabled\n")
            supplied = self.headers.get("X-Chaos-Token", "")
            if not hmac.compare_digest(supplied.encode(), state.token.encode()):
                return self._send(403, "forbidden\n")
            with state.lock:
                state.down = p.endswith("down")
                state.down_since = utcnow() if state.down else None
            return self._send(200, json.dumps({"role": state.role, "down": state.down, "at": utcnow()}) + "\n",
                              "application/json")

    return Handler


def make_server(role="primary", token="", host="0.0.0.0", port=8080):
    state = State(role, token)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    httpd.state = state
    return httpd


def run(role):
    port = int(os.environ.get("PORT", "8080"))
    srv = make_server(role, os.environ.get("CHAOS_TOKEN", ""), "0.0.0.0", port)
    print("%s role=%s listening on :%d chaos=%s" % (utcnow(), role, port, "enabled" if srv.state.token else "disabled"), flush=True)
    srv.serve_forever()
