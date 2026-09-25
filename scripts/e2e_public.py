#!/usr/bin/env python3
"""Public-mode browser journey against the REAL NexAgent server:
sign-up -> own API key -> MCP server + tool -> knowledge base with GraphRAG -> tool-using agent -> test run
-> Agent Team in the builder.

TEST DOUBLES: an imitation of the OpenAI API (tests/fake_providers.py) stands in for the model provider and a
real MCP SDK demo server (tests/mcp_demo_server.py) runs on localhost. Because both are on 127.0.0.1, this script
relaxes the "personal URLs must be public https" rule IN THIS PROCESS ONLY. Answers in screenshots are scripted.

Usage:  python scripts/e2e_public.py
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))
SHOTS = ROOT / "docs" / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)
PORT = 8612
PASSWORD = "public-e2e-password-1"

import fake_providers  # noqa: E402


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def main():
    script = fake_providers.Script()
    fake = fake_providers.start(script)
    fake_url = f"http://127.0.0.1:{fake.server_address[1]}"
    mcp_port = free_port()
    mcp = subprocess.Popen([sys.executable, str(ROOT / "backend/tests/mcp_demo_server.py"), "http", str(mcp_port)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    data = Path(tempfile.mkdtemp()) / "data"
    os.environ.update({"NEXAGENT_DATA_DIR": str(data), "NEXAGENT_PORT": str(PORT),
                       "NEXAGENT_ORIGIN": f"http://127.0.0.1:{PORT}", "NEXAGENT_PUBLIC_MODE": "1"})
    for k in ("NEXAGENT_OLLAMA_URL", "NEXAGENT_ALLOW_SIGNUP", "NEXAGENT_FEATURES"):
        os.environ.pop(k, None)
    from nexagent import llm
    from nexagent.api import connect
    relaxed = lambda url, personal=False: None      # noqa: E731  test doubles live on localhost
    llm.check_url = relaxed
    connect.check_url = relaxed
    from nexagent.main import init_state
    init_state()
    import uvicorn
    server = uvicorn.Server(uvicorn.Config("nexagent.main:app", host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", mcp_port), 0.2).close(); break
        except OSError:
            time.sleep(0.2)
    time.sleep(2)

    from playwright.sync_api import expect, sync_playwright
    base = f"http://127.0.0.1:{PORT}/"
    rc = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=_chromium())
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            # 1. sign up
            page.goto(base)
            page.get_by_role("button", name="Create an account").click()
            page.get_by_label("Username").fill("maya")
            page.get_by_label("Your name").fill("Maya (public user)")
            page.get_by_label("Password").fill(PASSWORD)
            page.screenshot(path=str(SHOTS / "p01-sign-up.png"))
            page.get_by_role("button", name="Create account").click()
            expect(page.get_by_text("Sign out")).to_be_visible()
            expect(page.get_by_role("button", name=r"Review queue")).to_have_count(0)
            # 2. own API key
            page.get_by_role("button", name="Models, keys & MCP tools").click()
            page.get_by_role("button", name="Add API key").click()
            page.locator("select", has=page.locator("option[value=openai-compatible]")).select_option("openai-compatible")
            page.get_by_label("Name", exact=True).fill("my-openai")
            page.get_by_label("Base URL (https)").fill(fake_url)
            page.get_by_label("API key", exact=True).fill("sk-test-key")
            page.get_by_role("button", name="Save").click()
            page.get_by_role("button", name="Test & list models").click()
            expect(page.get_by_text("key works")).to_be_visible()
            page.screenshot(path=str(SHOTS / "p02-my-api-keys.png"))
            # 3. MCP server
            page.get_by_role("tab", name="MCP servers & tools").click()
            page.get_by_role("button", name="Add MCP server").click()
            page.get_by_label("Name", exact=True).fill("plant-demo")
            page.get_by_label("Server URL").fill(f"http://127.0.0.1:{mcp_port}/mcp")
            page.get_by_role("button", name="Add & list tools").click()
            row = page.locator("tr", has_text="plant_status")
            expect(row).to_be_visible(timeout=15000)
            row.get_by_role("checkbox").click()
            expect(row.get_by_text("enabled")).to_be_visible()
            page.screenshot(path=str(SHOTS / "p03-mcp-tools.png"))
            # 4. knowledge base with GraphRAG
            page.get_by_role("button", name="Projects").click()
            page.get_by_text("My workspace").first.click()
            page.get_by_role("button", name="New knowledge base").click()
            page.get_by_label("Name", exact=True).fill("Unit manual")
            page.get_by_label("Embedding connection").select_option("my-openai")
            page.get_by_label("Embedding model").fill("fake-embed")
            page.get_by_label("Knowledge graph (GraphRAG)").select_option("rules")
            page.get_by_role("button", name="Create").click()
            page.get_by_text("Unit manual").first.click()
            page.locator("input[type=file]").first.set_input_files(str(ROOT / "samples" / "fictional-unit-manual.md"))
            page.get_by_role("button", name="Check & confirm").click(timeout=20000)
            page.get_by_role("button", name="Save & confirm for indexing").click()
            page.get_by_role("button", name="Build search index").click()
            expect(page.locator(".badge", has_text="in use")).to_be_visible(timeout=40000)
            page.get_by_role("tab", name="Knowledge graph").click()
            expect(page.locator(".react-flow__node").first).to_be_visible(timeout=10000)
            page.wait_for_timeout(600)
            page.screenshot(path=str(SHOTS / "p04-knowledge-graph.png"))
            # 5. tool-using agent
            page.get_by_role("tab", name="Agents").click()
            page.get_by_role("button", name="New agent").click()
            page.get_by_label("Tool-using agent").check()
            page.get_by_label("2. Name").fill("Plant helper")
            page.get_by_label("Answer model").fill("fake-model")
            page.get_by_role("button", name="Create and open builder").click()
            expect(page.locator(".react-flow__node")).to_have_count(3, timeout=15000)
            page.locator(".react-flow__node", has_text="Agent").first.click()
            page.get_by_label("plant-demo · plant_status").click()
            page.get_by_role("button", name="Save version").click()
            expect(page.get_by_text("valid", exact=True)).to_be_visible(timeout=10000)
            script.queue = [{"tool_calls": [{"name": "mcp_plant-demo__plant_status", "arguments": {"unit": "CDU-1"}}]},
                            {"tool_calls": [{"name": "search_knowledge", "arguments": {"query": "10-P-101A discharge pressure"}}]},
                            "CDU-1 is running normally, and the design discharge pressure of 10-P-101A is 5 bar [1]."]
            page.get_by_label("Test question").fill("Is CDU-1 running, and what is the discharge pressure of 10-P-101A?")
            page.get_by_role("button", name="Run test").click()
            expect(page.locator(".run-panel .badge", has_text="completed")).to_be_visible(timeout=30000)
            page.wait_for_timeout(500)
            page.screenshot(path=str(SHOTS / "p05-agent-with-mcp-tool.png"))
            called = [r["body"] for r in script.requests if r["path"] == "/v1/chat/completions"]
            assert any(m.get("role") == "tool" and "running normally" in str(m.get("content"))
                       for b in called for m in b["messages"]), ("MCP tool result never reached the model",
                [m for b in called for m in b["messages"] if m.get("role") == "tool"])
            # 6. agent team node in the builder
            page.get_by_role("button", name="Agent Team").first.click()
            page.locator(".react-flow__node", has_text="Agent Team").click()
            expect(page.get_by_text("Members (1/6)")).to_be_visible()
            page.get_by_role("button", name="Add member").click()
            expect(page.get_by_text("Members (2/6)")).to_be_visible()
            page.screenshot(path=str(SHOTS / "p06-agent-team-settings.png"))
        except Exception as exc:          # keep a screenshot of the failure
            page.screenshot(path=str(SHOTS / "p99-failure.png"))
            if os.environ.get("E2E_DEBUG"):
                print(page.evaluate("""async () => { const ps = await (await fetch('api/projects')).json();
                  const kbs = await (await fetch('api/projects/' + ps[0].id + '/knowledge-bases')).json();
                  const d = await (await fetch('api/knowledge-bases/' + kbs[0].id + '/documents')).json();
                  const g = await (await fetch('api/knowledge-bases/' + kbs[0].id + '/graph')).json();
                  return JSON.stringify({kbs, gens: d.generations, g: {status: g.status, n: g.entities.length}}); }"""))
            print("FAILED:", exc)
            rc = 1
        browser.close()
    real_errors = [e for e in errors if "401" not in e and "Failed to load resource" not in e]
    print("console errors:", real_errors or "none")
    mcp.kill()
    server.should_exit = True
    return 1 if (rc or real_errors) else 0


def _chromium() -> str:
    for c in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome")):
        return str(c)
    return ""


if __name__ == "__main__":
    sys.exit(main())
