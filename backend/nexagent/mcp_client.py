"""Minimal Model Context Protocol client (JSON-RPC 2.0) for tool use.

Transports:
  * http  — "Streamable HTTP": POST JSON-RPC to the server URL; the reply is JSON or an SSE stream;
            the Mcp-Session-Id header is kept for the session.
  * stdio — a local command started by the server (admin-registered only), newline-delimited JSON.

Only `initialize`, `tools/list` and `tools/call` are used. Each operation opens a short session and
closes it again, so no MCP process or connection outlives a request. Everything is bounded by timeouts.
Written against the protocol (not an SDK) so SDK API changes cannot break it; tested against a real
MCP SDK server in the test suite.
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import time
from dataclasses import dataclass

import httpx

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "nexagent-studio", "version": "0.3"}
MAX_RESULT_CHARS = 20000


class MCPError(Exception):
    pass


@dataclass
class ToolResult:
    text: str
    is_error: bool
    structured: dict | None = None


def _result_text(result: dict) -> ToolResult:
    parts = []
    for c in result.get("content") or []:
        if c.get("type") == "text":
            parts.append(c.get("text", ""))
        elif c.get("type") == "resource":
            res = c.get("resource") or {}
            parts.append(res.get("text") or f"[resource {res.get('uri', '')}]")
        else:
            parts.append(f"[{c.get('type', 'content')} omitted]")
    text = "\n".join(parts)
    if not text and result.get("structuredContent") is not None:
        text = json.dumps(result["structuredContent"], ensure_ascii=False)
    return ToolResult(text[:MAX_RESULT_CHARS], bool(result.get("isError")), result.get("structuredContent"))


# ------------------------------------------------------------------ HTTP
class HttpSession:
    def __init__(self, url: str, headers: dict | None = None, timeout: float = 60):
        self.url = url
        self.client = httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False,
                                   headers={"Accept": "application/json, text/event-stream",
                                            "Content-Type": "application/json", **(headers or {})})
        self.session_id: str | None = None
        self.next_id = 1

    def _post(self, payload: dict) -> dict | None:
        headers = {"MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        with self.client.stream("POST", self.url, json=payload, headers=headers) as r:
            if r.status_code in (401, 403):
                raise MCPError(f"MCP server refused access (HTTP {r.status_code}); check the authorization header")
            if r.status_code == 202:
                return None
            if r.status_code >= 300:
                r.read()
                raise MCPError(f"MCP server returned HTTP {r.status_code}: {r.text[:200]}")
            if r.headers.get("mcp-session-id"):
                self.session_id = r.headers["mcp-session-id"]
            ctype = r.headers.get("content-type", "")
            if "text/event-stream" in ctype:
                data_lines: list[str] = []
                for line in r.iter_lines():
                    if line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif line == "" and data_lines:
                        msg = json.loads("\n".join(data_lines))
                        data_lines = []
                        if msg.get("id") == payload.get("id"):
                            return msg
                if data_lines:
                    return json.loads("\n".join(data_lines))
                return None
            r.read()
            return r.json() if r.content else None

    def request(self, method: str, params: dict | None = None) -> dict:
        rid = self.next_id
        self.next_id += 1
        msg = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        if not msg:
            raise MCPError(f"No reply to {method}")
        if "error" in msg:
            raise MCPError(f"{method} failed: {msg['error'].get('message', msg['error'])}")
        return msg.get("result") or {}

    def notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        try:
            if self.session_id:
                self.client.delete(self.url, headers={"Mcp-Session-Id": self.session_id})
        except httpx.HTTPError:
            pass
        self.client.close()


# ------------------------------------------------------------------ stdio
class StdioSession:
    def __init__(self, command: str, args: list[str], env: dict | None = None, timeout: float = 60):
        base_env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "SYSTEMROOT") if k in os.environ}
        self.proc = subprocess.Popen([command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env={**base_env, **(env or {})}, text=True, bufsize=1)
        self.timeout = timeout
        self.next_id = 1

    def _send(self, obj: dict) -> None:
        if self.proc.poll() is not None:
            raise MCPError("The MCP server process exited")
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        rid = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.proc.stdout], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            line = self.proc.stdout.readline()
            if not line:
                raise MCPError("The MCP server process closed its output")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue                           # servers sometimes print logs to stdout
            if msg.get("id") == rid:
                if "error" in msg:
                    raise MCPError(f"{method} failed: {msg['error'].get('message', msg['error'])}")
                return msg.get("result") or {}
        raise MCPError(f"{method} timed out after {self.timeout:.0f} s")

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            self.proc.kill()


# ------------------------------------------------------------------ public helpers
def open_session(server: dict, secrets: dict, timeout: float = 60):
    if server["transport"] == "http":
        s = HttpSession(server["url"], secrets.get("headers"), timeout)
    else:
        s = StdioSession(server["command"], json.loads(server.get("args_json") or "[]"), secrets.get("env"), timeout)
    try:
        s.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO})
        s.notify("notifications/initialized")
    except Exception:
        s.close()
        raise
    return s


def list_tools(server: dict, secrets: dict) -> list[dict]:
    s = open_session(server, secrets, timeout=30)
    try:
        tools, cursor = [], None
        for _ in range(20):
            res = s.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools") or []
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools
    finally:
        s.close()


def call_tool(server: dict, secrets: dict, name: str, arguments: dict, timeout: float = 120) -> ToolResult:
    s = open_session(server, secrets, timeout=timeout)
    try:
        return _result_text(s.request("tools/call", {"name": name, "arguments": arguments or {}}))
    finally:
        s.close()
