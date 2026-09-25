#!/usr/bin/env python3
"""Browser journey for NexAgent Lite (the static, browser-only edition), run against the BUILT site (lite/dist).

TEST DOUBLES: OpenAI API and a remote MCP server are imitated with Playwright request routing (including CORS
preflights) — no real keys or servers are used. Real parts: the built app, IndexedDB, pdf.js, mammoth, the
search index, GraphRAG (graphology/Louvain), the agent loop and the answer checks.

Usage:  (cd lite && npm run build) && python scripts/e2e_lite.py
"""
from __future__ import annotations

import functools
import http.server
import json
import re
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "lite" / "dist"
FIX = ROOT / "lite" / "tests" / "fixtures"
SHOTS = ROOT / "docs" / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Expose-Headers": "Mcp-Session-Id"}


def embed(text: str) -> list[float]:
    v = [0.0] * 16
    for w in re.findall(r"\w+", text.lower()):
        v[sum(map(ord, w)) % 16] += 1
    return v


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".mjs": "text/javascript", ".js": "text/javascript"}

    def log_message(self, *a):
        pass


def main() -> int:
    if not (DIST / "index.html").exists():
        print("Build first: cd lite && npm run build")
        return 1
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(DIST)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/"

    script: list = []
    mcp_log: list[str] = []
    chat_bodies: list[dict] = []

    def ref_for(body) -> str:
        blob = json.dumps(body["messages"])
        m = re.search(r"\[(\d+)\] fictional-unit-manual\.md[^\[]*?5 bar", blob) or re.search(r"5 bar \[(\d+)\]", blob)
        return m.group(1) if m else "?"

    def openai(route):
        req = route.request
        if req.method == "OPTIONS":
            return route.fulfill(status=204, headers=CORS)
        if req.headers.get("authorization") != "Bearer sk-test-key":
            return route.fulfill(status=401, headers=CORS, body="{}")
        if req.url.endswith("/v1/models"):
            return route.fulfill(headers=CORS, json={"data": [{"id": "gpt-test"}, {"id": "text-embedding-3-small"}]})
        body = json.loads(req.post_data or "{}")
        if req.url.endswith("/v1/embeddings"):
            return route.fulfill(headers=CORS, json={"data": [{"index": i, "embedding": embed(t)} for i, t in enumerate(body["input"])]})
        chat_bodies.append(body)
        r = script.pop(0) if script else "The answer was not found in the accessible sources."
        if callable(r):
            r = r(body)
        if isinstance(r, str):
            msg = {"role": "assistant", "content": r}
        else:
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": c[0], "arguments": json.dumps(c[1])}} for i, c in enumerate(r)]}
        route.fulfill(headers=CORS, json={"choices": [{"message": msg}], "usage": {"prompt_tokens": 12, "completion_tokens": 8}})

    def mcp(route):
        req = route.request
        if req.method == "OPTIONS":
            return route.fulfill(status=204, headers=CORS)
        if req.method == "DELETE":
            return route.fulfill(status=204, headers=CORS)
        body = json.loads(req.post_data or "{}")
        mcp_log.append(body.get("method", ""))
        if "id" not in body:
            return route.fulfill(status=202, headers=CORS)
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "demo"}}
        elif body["method"] == "tools/list":
            result = {"tools": [{"name": "plant_status", "description": "Return the (fictional) running status of a unit",
                                 "inputSchema": {"type": "object", "properties": {"unit": {"type": "string"}}, "required": ["unit"]}},
                                {"name": "add", "description": "Add two numbers", "inputSchema": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}}}]}
        else:
            result = {"content": [{"type": "text", "text": f"Unit {body['params']['arguments'].get('unit')} is running normally (fictional)."}]}
        route.fulfill(headers={**CORS, "Content-Type": "text/event-stream", "Mcp-Session-Id": "sess-1"},
                      body=f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result})}\n\n")

    from playwright.sync_api import expect, sync_playwright
    rc = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=_chromium())
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        ctx.route("https://api.openai.com/**", openai)
        ctx.route("https://mcp.example.com/**", mcp)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(base)
            expect(page.get_by_text("Start by adding an API key")).to_be_visible()
            # 1. key
            page.locator(".side").get_by_role("button", name="API keys & data").click()
            page.get_by_role("button", name="Add API key").click()
            page.get_by_label("API key", exact=True).fill("sk-test-key")
            page.get_by_role("button", name="Save").click()
            page.get_by_role("button", name="Test").click()
            expect(page.get_by_text("key works")).to_be_visible()
            page.screenshot(path=str(SHOTS / "l01-lite-keys.png"))
            # 2. knowledge base with three formats + GraphRAG rules
            page.get_by_role("button", name="Knowledge & GraphRAG").click()
            page.get_by_role("button", name="New knowledge base").click()
            page.get_by_label("Name", exact=True).fill("Plant manuals")
            expect(page.get_by_label("Embedding model")).to_have_value("text-embedding-3-small")
            page.get_by_role("button", name="Create").click()
            page.get_by_label("Add documents").set_input_files([str(ROOT / "samples" / "fictional-unit-manual.md"),
                                                                str(FIX / "fictional-pump-handbook.pdf"), str(FIX / "fictional-valve-guide.docx")])
            for name in ("fictional-unit-manual.md", "fictional-pump-handbook.pdf", "fictional-valve-guide.docx"):
                expect(page.locator("tr", has_text=name)).to_be_visible(timeout=20000)
            page.get_by_role("button", name="Build index").click()
            expect(page.locator(".page-head .badge", has_text="ready")).to_be_visible(timeout=30000)
            page.screenshot(path=str(SHOTS / "l02-lite-knowledge.png"))
            page.get_by_role("tab", name="Try search").click()
            page.get_by_label("Search query").fill("maximum flow of 30-FV-301")
            page.get_by_role("button", name="Search").click()
            expect(page.locator(".source").first).to_contain_text("45 m3/h")          # read from the .docx table
            page.get_by_label("Search query").fill("seal inspection 20-P-201B")
            page.get_by_role("button", name="Search").click()
            expect(page.locator(".source").first).to_contain_text("20-P-201B")        # read from the PDF
            page.get_by_role("tab", name="Knowledge graph").click()
            expect(page.locator(".react-flow__node").first).to_be_visible()
            page.wait_for_timeout(500)
            page.screenshot(path=str(SHOTS / "l03-lite-graph.png"))
            # 3. MCP server
            page.get_by_role("button", name="MCP tools").click()
            page.get_by_role("button", name="Add MCP server").click()
            page.get_by_label("Name", exact=True).fill("plant")
            page.get_by_label("Server URL").fill("https://mcp.example.com/mcp")
            page.get_by_role("button", name="Add & list tools").click()
            row = page.locator("tr", has_text="plant_status")
            expect(row).to_be_visible()
            row.get_by_role("checkbox").click()
            expect(row.get_by_text("enabled")).to_be_visible()
            # 4. agent team
            page.get_by_role("button", name="Agent builder").click()
            page.get_by_role("button", name="New agent").click()
            page.get_by_role("button", name=re.compile("Agent team")).click()
            page.get_by_label("Model", exact=True).fill("gpt-test")
            page.locator(".react-flow__node", has_text="researcher").click()
            page.get_by_label("Plant manuals").check()
            page.get_by_label("plant · plant_status").check()
            page.get_by_role("button", name="Save").click()
            expect(page.get_by_text("ready", exact=True)).to_be_visible()
            page.screenshot(path=str(SHOTS / "l04-lite-team-builder.png"))
            page.get_by_role("button", name="Chat with it →").click()
            script.extend([
                [("delegate_to_researcher", {"task": "Find the discharge pressure of 10-P-101A and whether CDU-1 runs"})],
                [("search_knowledge", {"query": "10-P-101A discharge pressure"})],
                [("mcp_plant__plant_status", {"unit": "CDU-1"})],
                lambda b: f"10-P-101A: 5 bar [{ref_for(b)}]. CDU-1 is running normally.",
                lambda b: f"The design discharge pressure of 10-P-101A is 5 bar [{ref_for(b)}]. CDU-1 is running normally (fictional MCP tool).",
            ])
            page.get_by_label("Your question").fill("Is CDU-1 running, and what is the discharge pressure of 10-P-101A?")
            page.get_by_role("button", name="Ask").click()
            expect(page.get_by_text("checks passed")).to_be_visible(timeout=20000)
            expect(page.locator(".source").first).to_contain_text("5 bar")
            page.get_by_text(re.compile(r"How it worked")).click()
            page.screenshot(path=str(SHOTS / "l05-lite-chat.png"), full_page=True)
            assert mcp_log.count("tools/call") == 1, mcp_log
            # an unsupported number is flagged
            script.append("The discharge pressure of 10-P-101A is 9 bar.")
            page.get_by_label("Your question").fill("And the maximum?")
            page.get_by_role("button", name="Ask").click()
            expect(page.get_by_text("Not supported by the sources")).to_be_visible(timeout=20000)
            # 5. persistence across reload
            page.reload()
            page.get_by_role("button", name="Chat with agents").click()
            expect(page.get_by_text("Is CDU-1 running").first).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(300)
            page.screenshot(path=str(SHOTS / "l06-lite-mobile.png"))
        except Exception as exc:
            page.screenshot(path=str(SHOTS / "l99-failure.png"))
            print("FAILED:", exc)
            rc = 1
        browser.close()
    real = [e for e in errors if "401" not in e and "Failed to load resource" not in e]
    print("console errors:", real or "none")
    httpd.shutdown()
    return 1 if (rc or real) else 0


def _chromium() -> str:
    for c in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome")):
        return str(c)
    return ""


if __name__ == "__main__":
    sys.exit(main())
