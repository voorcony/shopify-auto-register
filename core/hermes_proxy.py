#!/usr/bin/env python3
"""Local proxy: strips reasoning_content from DeepSeek API traffic."""

import json
import http.server
import urllib.request
import ssl
import sys
import os

TARGET = "https://api.deepseek.com/v1"
PORT = 18888

def strip_reasoning(obj):
    """Recursively strip reasoning_content from all messages."""
    if isinstance(obj, dict):
        obj.pop("reasoning_content", None)
        for v in obj.values():
            strip_reasoning(v)
    elif isinstance(obj, list):
        for item in obj:
            strip_reasoning(item)

class Proxy(http.server.BaseHTTPRequestHandler):
    def do_request(self, method):
        path = self.path
        body_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(body_len) if body_len else b""

        # Modify request: add thinking disabled
        if body:
            try:
                data = json.loads(body)
                data["thinking"] = {"type": "disabled"}
                body = json.dumps(data).encode()
            except:
                pass

        url = TARGET + path
        req = urllib.request.Request(url, data=body, method=method)
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length", "connection"):
                req.add_header(k, v)

        ctx = ssl.create_default_context()
        try:
            resp = urllib.request.urlopen(req, timeout=120, context=ctx)
            resp_body = resp.read()
            resp_headers = dict(resp.headers)
        except urllib.error.HTTPError as e:
            resp_body = e.read()
            resp_headers = dict(e.headers)
            status = e.code
        else:
            status = resp.status

        # Strip reasoning_content from response
        if resp_body:
            try:
                data = json.loads(resp_body)
                strip_reasoning(data)
                resp_body = json.dumps(data).encode()
            except:
                pass

        self.send_response(status)
        for k, v in resp_headers.items():
            if k.lower() not in ("transfer-encoding", "content-encoding", "connection"):
                self.send_header(k, v)
        self.send_header("Content-Length", len(resp_body))
        self.end_headers()
        self.wfile.write(resp_body)

    do_POST = do_request
    do_GET = do_request

    def log_message(self, fmt, *args):
        print(f"[proxy] {args[0]}", file=sys.stderr)

if __name__ == "__main__":
    print(f"Proxy: 127.0.0.1:{PORT} -> {TARGET}", file=sys.stderr)
    server = http.server.HTTPServer(("127.0.0.1", PORT), Proxy)
    server.serve_forever()
