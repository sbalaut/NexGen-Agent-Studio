"""TEST DOUBLE — one local HTTP server imitating the OpenAI, Anthropic and Gemini REST APIs.

Used only by the automated tests to check that NexAgent sends correctly shaped requests
(auth headers, tool definitions, tool results) and parses replies, without real API keys.
Replies come from `script.queue` (text or {"tool_calls": [...]}) — the last one repeats.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fake_ollama import embed


class Script:
    def __init__(self):
        self.queue: list = ["Hello from the fake provider."]
        self.requests: list[dict] = []

    def next(self):
        return self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]


def start(script: Script):
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

        def _auth_ok(self):
            h = self.headers
            return (h.get("Authorization") == "Bearer sk-test-key" or h.get("x-api-key") == "sk-test-key"
                    or h.get("x-goog-api-key") == "sk-test-key")

        def do_GET(self):
            if not self._auth_ok():
                return self._send(401, {"error": "bad key"})
            if self.path.startswith("/v1/models"):
                return self._send(200, {"data": [{"id": "fake-model"}, {"id": "fake-embed"}]})
            if self.path.startswith("/v1beta/models"):
                return self._send(200, {"models": [{"name": "models/fake-model", "supportedGenerationMethods": ["generateContent"]},
                                                   {"name": "models/fake-embed", "supportedGenerationMethods": ["embedContent"]}]})
            self._send(404, {})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            script.requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
            if not self._auth_ok():
                return self._send(401, {"error": "bad key"})
            if self.path == "/v1/embeddings":
                return self._send(200, {"data": [{"index": i, "embedding": embed(t)} for i, t in enumerate(body["input"])]})
            if self.path.endswith(":batchEmbedContents"):
                return self._send(200, {"embeddings": [{"values": embed(r["content"]["parts"][0]["text"])} for r in body["requests"]]})
            reply = script.next()
            calls = reply.get("tool_calls", []) if isinstance(reply, dict) else []
            text = reply.get("content", "") if isinstance(reply, dict) else reply
            if self.path == "/v1/chat/completions":
                msg = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = [{"id": f"c{i}", "type": "function",
                                          "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments", {}))}}
                                         for i, c in enumerate(calls)]
                return self._send(200, {"choices": [{"message": msg}], "usage": {"total_tokens": 10}})
            if self.path == "/v1/messages":
                content = ([{"type": "text", "text": text}] if text else []) + \
                          [{"type": "tool_use", "id": f"tu{i}", "name": c["name"], "input": c.get("arguments", {})}
                           for i, c in enumerate(calls)]
                return self._send(200, {"content": content, "stop_reason": "tool_use" if calls else "end_turn",
                                        "usage": {"input_tokens": 5, "output_tokens": 5}})
            if self.path.endswith(":generateContent"):
                parts = ([{"text": text}] if text else []) + [{"functionCall": {"name": c["name"], "args": c.get("arguments", {})}}
                                                               for c in calls]
                return self._send(200, {"candidates": [{"content": {"role": "model", "parts": parts}}]})
            self._send(404, {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
