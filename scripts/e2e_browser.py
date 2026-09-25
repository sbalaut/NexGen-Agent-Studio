#!/usr/bin/env python3
"""Browser journey against the REAL NexAgent server (real SQLite, real executor, real
validators, real job workers) with the Ollama TEST DOUBLE standing in for the model,
because no model weights are available in the build environment.

Screenshots written to docs/screenshots/ are labelled accordingly: answers shown in them
come from the scripted test double, not from a language model.

Usage:  python scripts/e2e_browser.py
"""
from __future__ import annotations

import os
import re
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
PORT = 8611
PASSWORD = "e2e-password-1234"

import fake_ollama  # noqa: E402

script = fake_ollama.Script()


def responder(messages):
    """Scripted 'model': cites the source that holds the asked-about tag. For 10-C-101 it deliberately
    swaps in the pump's value so the review path can be shown."""
    user = messages[-1]["content"]
    if "A reviewer found these problems" in user:          # repair attempt: still wrong on purpose
        user = messages[1]["content"]
    q = user.split("QUESTION:")[-1]
    sources = re.findall(r'<source ref="(\d+)"[^>]*>\n(.*?)\n</source>', messages[1]["content"], re.S)
    def ref_for(tag):
        for ref, text in sources:
            if tag in text and ("bar" in text or "°C" in text):
                return ref
        return None
    if "10-P-101A" in q and ref_for("10-P-101A"):
        return f"The design discharge pressure of 10-P-101A is 5 bar [{ref_for('10-P-101A')}]."
    if "10-C-101" in q and ref_for("10-P-101A"):
        return f"The design discharge pressure of 10-C-101 is 5 bar [{ref_for('10-P-101A')}]."
    return "The answer was not found in the accessible sources."


def main():
    script.responder = responder
    fake = fake_ollama.start(script)
    data = Path(tempfile.mkdtemp()) / "data"
    os.environ.update({"NEXAGENT_DATA_DIR": str(data), "NEXAGENT_PORT": str(PORT),
                       "NEXAGENT_ORIGIN": f"http://127.0.0.1:{PORT}",
                       "NEXAGENT_OLLAMA_URL": f"http://127.0.0.1:{fake.server_address[1]}",
                       "NEXAGENT_GENERATION_MODEL": "test-gen", "NEXAGENT_REVIEW_MODEL": "test-gen",
                       "NEXAGENT_EMBEDDING_MODEL": "test-embed"})
    from nexagent import db
    from nexagent.main import init_state
    from nexagent.security import hash_password
    init_state()
    for name, roles in (("asha", ["Admin", "Builder"]), ("ravi", ["Viewer"]), ("meena", ["Reviewer"]),
                        ("kiran", ["TrainingOperator"])):
        uid = db.new_id()
        with db.tx() as conn:
            conn.execute("INSERT INTO users(id,username,display_name,password_hash,active,created_at) VALUES(?,?,?,?,1,?)",
                         (uid, name, {"asha": "Asha (builder)", "ravi": "Ravi (operator)", "meena": "Meena (engineer)",
                                      "kiran": "Kiran (training)"}[name], hash_password(PASSWORD), db.now()))
            conn.executemany("INSERT INTO user_roles VALUES(?,?)", [(uid, r) for r in roles])
    import uvicorn
    server = uvicorn.Server(uvicorn.Config("nexagent.main:app", host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(2.5)

    from playwright.sync_api import sync_playwright, expect
    base = f"http://127.0.0.1:{PORT}/"
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=_chromium())
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        def login(user):
            page.goto(base)
            page.get_by_label("Username").fill(user)
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            expect(page.get_by_text("Sign out")).to_be_visible()

        def logout():
            page.get_by_role("button", name="Sign out").click()
            expect(page.get_by_role("button", name="Sign in")).to_be_visible()

        page.goto(base)
        page.screenshot(path=str(SHOTS / "01-sign-in.png"))
        login("asha")
        page.get_by_role("button", name="New project").click()
        page.get_by_label("Name").fill("CDU-1 operations")
        page.get_by_label("Description").fill("Assistants for the fictional Cedar crude unit (training material).")
        page.get_by_role("button", name="Create").click()
        page.get_by_role("button", name="New knowledge base").click()
        page.get_by_label("Name").fill("Unit manual")
        page.get_by_role("button", name="Create").click()
        page.get_by_text("Unit manual").first.click()
        page.locator('input[type=file]').first.set_input_files(str(ROOT / "samples" / "fictional-unit-manual.md"))
        page.get_by_role("button", name="Check & confirm").click(timeout=20000)
        expect(page.get_by_text("Equipment specifications").first).to_be_visible()
        page.screenshot(path=str(SHOTS / "02-extraction-preview.png"))
        page.get_by_role("button", name="Save & confirm for indexing").click()
        page.get_by_role("button", name="Build search index").click()
        expect(page.locator(".badge", has_text="in use")).to_be_visible(timeout=30000)
        page.screenshot(path=str(SHOTS / "03-knowledge-base.png"))
        # members + access
        page.get_by_role("tab", name="Members").click()
        for u in ("ravi", "meena"):
            page.get_by_label("Account").select_option(u)
            page.get_by_role("button", name="Add", exact=True).click()
            expect(page.get_by_text("@" + u).first).to_be_visible()
        page.get_by_role("tab", name="Knowledge bases").click()
        page.get_by_text("Unit manual").first.click()
        page.get_by_role("button", name="Who can read this").click()
        for u in ("ravi", "meena"):
            page.locator("tr", has_text="@" + u).get_by_role("button", name="read").click()
            page.wait_for_timeout(300)
        page.keyboard.press("Escape")
        # agent
        page.get_by_role("tab", name="Agents").click()
        page.get_by_role("button", name="New agent").click()
        page.get_by_label("2. Name").fill("Manual Q&A")
        page.get_by_role("button", name="Create and open builder").click()
        expect(page.locator(".react-flow__node")).to_have_count(6, timeout=15000)
        page.wait_for_timeout(800)
        page.screenshot(path=str(SHOTS / "04-agent-builder-canvas.png"))
        page.locator(".react-flow__node", has_text="Answer Review").click()
        page.wait_for_timeout(300)
        page.get_by_label("Test question").fill("What is the design discharge pressure of 10-P-101A?")
        page.get_by_role("button", name="Run test").click()
        expect(page.locator(".run-panel .badge", has_text="completed")).to_be_visible(timeout=30000)
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "05-builder-test-run.png"))
        page.get_by_role("tab", name="List & form view").click()
        page.screenshot(path=str(SHOTS / "06-builder-list-view.png"), full_page=True)
        page.get_by_role("tab", name="Canvas").click()
        # publish
        page.locator(".crumbs button").click()
        page.get_by_role("tab", name="Published assistants").click()
        page.get_by_role("button", name="Publish an agent").click()
        page.get_by_label("Assistant name").fill("CDU manual assistant")
        page.get_by_label("Address (lowercase, digits, hyphens)").fill("cdu-manual")
        page.get_by_role("button", name="Publish", exact=True).click()
        expect(page.get_by_text("/cdu-manual")).to_be_visible()
        page.screenshot(path=str(SHOTS / "07-published.png"))
        logout()
        # operator asks
        login("ravi")
        page.get_by_role("button", name="Assistants").click()
        page.get_by_label("Your question").fill("What is the design discharge pressure of 10-P-101A?")
        page.get_by_role("button", name="Ask").click()
        expect(page.locator(".bubble-a .source").first).to_be_visible(timeout=30000)
        page.screenshot(path=str(SHOTS / "08-chat-verified-answer.png"))
        page.get_by_label("Your question").fill("What is the design discharge pressure of 10-C-101?")
        page.get_by_role("button", name="Ask").click()
        expect(page.get_by_text("An engineer is reviewing this answer")).to_be_visible(timeout=30000)
        page.screenshot(path=str(SHOTS / "09-chat-sent-to-engineer.png"))
        logout()
        # engineer reviews
        login("meena")
        page.get_by_role("button", name=re.compile("Review queue")).click()
        page.get_by_text("10-C-101").first.click()
        expect(page.get_by_text("Why the checks flagged it")).to_be_visible()
        page.screenshot(path=str(SHOTS / "10-engineer-review.png"), full_page=True)
        ref = page.locator(".source", has_text="10-C-101 | Property").locator(".source-ref").first.inner_text()
        page.get_by_label("Corrected answer").fill(
            f"The manual gives no discharge pressure for 10-C-101. Its design reference temperature is 120 °C [{ref}].")
        page.get_by_role("button", name="Run built-in checks on my text").click()
        page.wait_for_timeout(600)
        page.get_by_role("button", name="Save correction & release").click()
        page.wait_for_timeout(800)
        logout()
        login("ravi")
        page.get_by_role("button", name="Assistants").click()
        page.get_by_text("What is the design discharge pressure of 10-C-101?").first.click()
        expect(page.get_by_text("engineer-reviewed").first).to_be_visible(timeout=10000)
        page.screenshot(path=str(SHOTS / "11-chat-engineer-answer.png"))
        logout()
        login("asha")
        page.get_by_role("button", name="Users, models & audit").click()
        page.screenshot(path=str(SHOTS / "12-admin-users.png"))
        page.get_by_role("tab", name="Audit log").click()
        page.screenshot(path=str(SHOTS / "13-audit-log.png"))
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(base + "#/chat")
        page.wait_for_timeout(800)
        page.screenshot(path=str(SHOTS / "14-mobile-assistants.png"))
        browser.close()
    real_errors = [e for e in errors if "401" not in e and "Failed to load resource" not in e]
    print("console errors:", real_errors or "none")
    print("screenshots:", sorted(x.name for x in SHOTS.glob("*.png")))
    server.should_exit = True
    return 1 if real_errors else 0


def _chromium() -> str:
    for c in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome")):
        return str(c)
    return ""


if __name__ == "__main__":
    sys.exit(main())
