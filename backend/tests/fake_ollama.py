"""TEST DOUBLE — a tiny HTTP server that imitates Ollama's /api/chat, /api/embed and /api/tags.

It is used ONLY by the automated tests (it is not shipped in the app and the app
never falls back to it). Replies are scripted by the test so that validator,
review-routing, cancellation and policy behaviour can be checked
deterministically without a GPU or model download.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np


class Script:
    def __init__(self):
        self.answers: list[str] = []          # queue of generation replies (last one repeats)
        self.review = {"verdict": "pass", "issues": [], "missing_evidence": [], "suggested_action": ""}
        self.review_raises = False
        self.delay_s = 0.0
        self.requests: list[dict] = []
        self.responder = None                  # optional callable(messages) -> str | {"tool_calls": [...]}
        self.json_responder = None             # optional callable(messages) -> dict for JSON-mode requests

    def next_answer(self) -> str:
        if len(self.answers) > 1:
            return self.answers.pop(0)
        return self.answers[0] if self.answers else "The answer was not found in the accessible sources."


def embed(text: str, dim: int = 64) -> list[float]:
    v = np.zeros(dim, dtype=np.float32)
    for tok in re.findall(r"\w+", text.lower()):
        v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % dim] += 1.0
    return v.tolist()


def start(script: Script, port: int = 0):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/tags":
                return self._send(200, {"models": [{"name": "test-gen:latest"}, {"name": "test-embed:latest"}]})
            self._send(404, {})

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            script.requests.append({"path": self.path, "body": data})
            if self.path == "/api/embed":
                if data.get("model") == "missing-embed":
                    return self._send(404, {"error": "model not found"})
                return self._send(200, {"embeddings": [embed(t) for t in data["input"]]})
            if self.path == "/api/chat":
                if data.get("model") == "missing-model":
                    return self._send(404, {"error": "model not found"})
                time.sleep(script.delay_s)
                if data.get("format") == "json" and script.json_responder:
                    return self._send(200, {"message": {"content": json.dumps(script.json_responder(data["messages"]))}})
                if data.get("format") == "json":
                    if script.review_raises:
                        return self._send(500, {"error": "reviewer crashed"})
                    return self._send(200, {"message": {"content": json.dumps(script.review)}})
                reply = script.responder(data["messages"]) if script.responder else script.next_answer()
                if isinstance(reply, dict):          # scripted tool call(s)
                    return self._send(200, {"message": {"content": reply.get("content", ""), "tool_calls": [
                        {"function": {"name": c["name"], "arguments": c.get("arguments", {})}} for c in reply["tool_calls"]]}})
                return self._send(200, {"message": {"content": reply}})
            self._send(404, {})

    server = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
