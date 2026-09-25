"""End-to-end API tests against real SQLite, the real executor and the Ollama TEST DOUBLE."""
import json
import time

import pytest

from conftest import ORIGIN, PASSWORD, SAMPLES, client_for, drain, make_user
from nexagent import db, security

GOOD = "The design discharge pressure of 10-P-101A is 5 bar [{ref}]."
SWAPPED = "The design discharge pressure of 10-C-101 is 5 bar [{ref}]."


@pytest.fixture()
def world(env):
    ids = {
        "admin": make_user("admin", ["Admin", "Builder"]),
        "builder": make_user("builder", ["Builder"]),
        "viewer": make_user("viewer", ["Viewer"]),
        "outsider": make_user("outsider", ["Viewer"]),
        "reviewer": make_user("reviewer", ["Reviewer"]),
        "trainer": make_user("trainer", ["TrainingOperator", "Reviewer"]),
        "trainer2": make_user("trainer2", ["TrainingOperator"]),
        "promoter": make_user("promoter", ["Viewer"], ["model.promote"]),
    }
    b = client_for("builder")
    pid = b.post("/api/projects", json={"name": "CDU", "classification_floor": "Internal"}).json()["id"]
    kid = b.post(f"/api/projects/{pid}/knowledge-bases", json={"name": "Unit manual", "classification": "Internal"}).json()["id"]
    for u, m in (("viewer", "viewer"), ("reviewer", "viewer"), ("trainer", "viewer"), ("trainer2", "viewer"),
                 ("promoter", "viewer")):
        assert b.put(f"/api/projects/{pid}/members", json={"username": u, "membership": m}).status_code == 200
    for u in ("viewer", "reviewer", "trainer", "trainer2"):
        assert b.put(f"/api/knowledge-bases/{kid}/grants", json={"user_id": ids[u], "can_read": True}).status_code == 200
    data = (SAMPLES / "fictional-unit-manual.md").read_bytes()
    up = b.post(f"/api/knowledge-bases/{kid}/documents", files={"file": ("unit-manual.md", data, "text/markdown")},
                data={"classification": "Internal"})
    assert up.status_code == 201, up.text
    rid = up.json()["revision_id"]
    drain()
    prev = b.get(f"/api/revisions/{rid}/preview").json()
    assert prev["revision"]["status"] == "preview_ready"
    assert b.post(f"/api/revisions/{rid}/confirm").status_code == 200
    assert b.post(f"/api/knowledge-bases/{kid}/index").status_code == 200
    drain()
    wf = b.post(f"/api/projects/{pid}/workflows", json={"name": "Manual Q&A", "kb_ids": [kid]}).json()
    assert wf["valid"], wf
    return {"ids": ids, "b": b, "pid": pid, "kid": kid, "wid": wf["id"], "rid": rid, "doc": up.json()["document_id"],
            **env}


def run_to_end(client, run_id):
    drain()
    return client.get(f"/api/runs/{run_id}").json()


# ------------------------------------------------------------------ auth & throttling
def test_login_backoff_is_per_account_not_site_wide(env):
    make_user("alice", ["Viewer"]); make_user("bob", ["Viewer"])
    from fastapi.testclient import TestClient
    from nexagent.main import app
    c = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    for _ in range(security.ACCOUNT_ALLOWANCE + 1):
        assert c.post("/api/auth/login", json={"username": "alice", "password": "wrong-password"}).status_code == 401
    locked = c.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert locked.status_code == 429 and "Retry-After" in locked.headers
    assert c.post("/api/auth/login", json={"username": "bob", "password": PASSWORD}).status_code == 200


def test_lock_grows_exponentially_and_is_capped():
    assert security.lock_seconds(5, 5) == 0
    assert security.lock_seconds(6, 5) == 30 and security.lock_seconds(7, 5) == 60
    assert security.lock_seconds(40, 5) == security.MAX_LOCK_S


def test_csrf_and_origin_required(world):
    b = world["b"]
    token = b.headers.pop("X-CSRF-Token")
    assert b.post("/api/projects", json={"name": "x"}).status_code == 403
    b.headers["X-CSRF-Token"] = token
    b.headers["Origin"] = "https://evil.example"
    assert b.post("/api/projects", json={"name": "x"}).status_code == 403
    b.headers["Origin"] = ORIGIN


def test_security_headers(world):
    r = world["b"].get("/api/auth/me")
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"


# ------------------------------------------------------------------ documents
def test_preview_blocks_keep_table_rows_and_sections(world):
    p = world["b"].get(f"/api/revisions/{world['rid']}/preview").json()
    rows = [b for b in p["blocks"] if b["kind"] == "table_row"]
    assert any("10-P-101A" in r["text"] and "5 bar" in r["text"] for r in rows)
    types = {b["heading"]: b["section_type"] for b in p["blocks"]}
    assert types["Interlock"] == "interlock" and types["Startup"] == "startup"
    assert types["Process description"] == "process_description"


def test_scanned_pdf_reported(world):
    from reportlab.pdfgen import canvas
    path = world["tmp"] / "scan.pdf"
    c = canvas.Canvas(str(path)); c.rect(10, 10, 100, 100); c.showPage(); c.save()
    b = world["b"]
    r = b.post(f"/api/knowledge-bases/{world['kid']}/documents", files={"file": ("scan.pdf", path.read_bytes(), "application/pdf")})
    drain()
    prev = b.get(f"/api/revisions/{r.json()['revision_id']}/preview").json()
    assert prev["revision"]["status"] == "scanned_ocr_deferred"
    assert "OCR" in prev["revision"]["error"]


def test_failed_index_keeps_previous_generation(world):
    b, kid = world["b"], world["kid"]
    active = b.get(f"/api/knowledge-bases/{kid}/documents").json()["active_generation"]
    with db.tx() as conn:
        db.set_setting(conn, "embedding_model", "missing-embed")
    world["script"].requests.clear()
    import nexagent.llm as llm
    orig = llm.Gateway.embed
    llm.Gateway.embed = lambda self, *a, **k: (_ for _ in ()).throw(llm.ModelUnavailable("embedding model missing"))
    try:
        b.post(f"/api/knowledge-bases/{kid}/index"); drain()
    finally:
        llm.Gateway.embed = orig
    info = b.get(f"/api/knowledge-bases/{kid}/documents").json()
    assert info["active_generation"] == active
    assert info["generations"][0]["status"] == "failed"


# ------------------------------------------------------------------ graph validation
def test_graph_cannot_bypass_review(world):
    b, wid = world["b"], world["wid"]
    wf = b.get(f"/api/workflows/{wid}").json()
    g = wf["version"]["graph"]
    g["edges"] = [e for e in g["edges"] if e["target"] != "output"]
    g["edges"].append({"id": "bad", "source": "llm", "sourceHandle": "draft", "target": "output", "targetHandle": "answer"})
    res = b.post(f"/api/workflows/{wid}/validate", json={"graph": g, "base_version": 1}).json()
    assert not res["valid"] and any("expects reviewed" in e for e in res["errors"])


def test_graph_rejects_cycles_unknown_nodes_and_foreign_kbs(world):
    b, wid = world["b"], world["wid"]
    g = b.get(f"/api/workflows/{wid}").json()["version"]["graph"]
    g2 = json.loads(json.dumps(g)); g2["nodes"].append({"id": "x", "type": "shell", "data": {}})
    assert "Unknown node type" in b.post(f"/api/workflows/{wid}/validate", json={"graph": g2, "base_version": 1}).json()["errors"][0]
    g3 = json.loads(json.dumps(g))
    g3["nodes"][1]["data"]["kb_ids"] = ["not-mine"]
    assert any("do not have access" in e for e in
               b.post(f"/api/workflows/{wid}/validate", json={"graph": g3, "base_version": 1}).json()["errors"])
    g4 = json.loads(json.dumps(g))
    g4["nodes"].append({"id": "c", "type": "condition", "data": {"rule": "text_contains", "argument": "a"}})
    g4["edges"] += [{"id": "l1", "source": "input", "sourceHandle": "question", "target": "c", "targetHandle": "value"},
                    {"id": "l2", "source": "c", "sourceHandle": "true", "target": "c", "targetHandle": "value"}]
    errs = b.post(f"/api/workflows/{wid}/validate", json={"graph": g4, "base_version": 1}).json()["errors"]
    assert errs


def test_version_conflict_detected(world):
    b, wid = world["b"], world["wid"]
    g = b.get(f"/api/workflows/{wid}").json()["version"]["graph"]
    assert b.post(f"/api/workflows/{wid}/versions", json={"graph": g, "base_version": 1}).status_code == 200
    assert b.post(f"/api/workflows/{wid}/versions", json={"graph": g, "base_version": 1}).status_code == 409


# ------------------------------------------------------------------ execution & review
def _probe_rows(world, question):
    """Run once to learn which numbered sources the server retrieves (refs are assigned by the server)."""
    b = world["b"]
    world["script"].answers = ["The answer was not found in the accessible sources."]
    run = run_to_end(b, b.post(f"/api/workflows/{world['wid']}/test", json={"question": question}).json()["run_id"])
    assert run["status"] == "not_found"
    return [n for n in run["nodes"] if n["node_type"] == "retrieval"][0]["detail"]["evidence"]


def test_good_answer_released_with_server_built_citations(world):
    b, q = world["b"], "What is the design discharge pressure of 10-P-101A?"
    released = []
    for r in _probe_rows(world, q):
        world["script"].answers = [GOOD.format(ref=r["ref"])]
        run = run_to_end(b, b.post(f"/api/workflows/{world['wid']}/test", json={"question": q}).json()["run_id"])
        if run["status"] == "completed":
            released.append(run)
        else:
            assert run["status"] == "awaiting_review"      # wrong source number -> engineer, never released
    assert len(released) == 1, "exactly the source that binds pump + value + condition must be accepted"
    cit = released[0]["answer"]["citations"]
    assert "10-P-101A" in cit[0]["text"] and cit[0]["filename"] == "unit-manual.md" and cit[0]["revision_no"] == 1


def test_swapped_value_goes_to_engineer_and_stream_has_no_draft(world):
    b = world["b"]
    q = "What is the design discharge pressure of 10-C-101?"
    world["script"].answers = [SWAPPED.format(ref=1)]
    rid = b.post(f"/api/workflows/{world['wid']}/test", json={"question": q}).json()["run_id"]
    run = run_to_end(b, rid)
    assert run["status"] == "awaiting_review"
    assert run["answer"]["answer"] == ""
    review_node = [n for n in run["nodes"] if n["node_type"] == "review"][0]
    assert "after one repair" in review_node["summary"]
    with db.read() as conn:
        events = [json.loads(r[0]) for r in conn.execute("SELECT data FROM run_events WHERE run_id=?", (rid,))]
    assert not any("10-C-101 is 5 bar" in json.dumps(e) for e in events), "draft text must never be streamed"


def test_repair_then_pass(world):
    b, q = world["b"], "What is the design discharge pressure of 10-P-101A?"
    for r in _probe_rows(world, q):
        world["script"].answers = [SWAPPED.format(ref=r["ref"]), GOOD.format(ref=r["ref"])]
        run = run_to_end(b, b.post(f"/api/workflows/{world['wid']}/test", json={"question": q}).json()["run_id"])
        if run["status"] == "completed":
            node = [n for n in run["nodes"] if n["node_type"] == "review"][0]
            assert "after one repair" in node["summary"]
            return
    pytest.fail("repair path never produced a released answer")


def test_reviewer_unavailable_means_human_review(world):
    world["script"].review_raises = True
    b = world["b"]
    world["script"].answers = [GOOD.format(ref=1)]
    run = run_to_end(b, b.post(f"/api/workflows/{world['wid']}/test",
                                json={"question": "design discharge pressure 10-P-101A"}).json()["run_id"])
    assert run["status"] == "awaiting_review"


def test_unauthorized_user_cannot_retrieve_or_view(world):
    b = world["b"]
    pub = b.post(f"/api/projects/{world['pid']}/assistants",
                 json={"workflow_id": world["wid"], "name": "Manual bot", "slug": "manual-bot"}).json()
    # 'promoter' is a project viewer WITHOUT a KB grant: retrieval must return nothing
    p = client_for("promoter")
    world["script"].answers = ["The answer was not found in the accessible sources."]
    run = run_to_end(p, p.post(f"/api/assistants/{pub['id']}/ask", json={"question": "10-P-101A pressure"}).json()["run_id"])
    assert run["status"] == "not_found"
    gen_prompts = [r for r in world["script"].requests if r["path"] == "/api/chat" and "10-P-101A |" in json.dumps(r["body"])]
    assert not gen_prompts, "no source text may reach the model for a user without access"
    o = client_for("outsider")
    assert o.post(f"/api/assistants/{pub['id']}/ask", json={"question": "x"}).status_code == 404
    assert o.get(f"/api/runs/{run['id']}").status_code == 404
    with db.read() as conn:
        chunk = conn.execute("SELECT id FROM chunks LIMIT 1").fetchone()[0]
    assert o.get(f"/api/sources/{chunk}").status_code == 404
    assert p.get(f"/api/sources/{chunk}").status_code == 404


def test_restricted_content_never_sent_to_external_endpoint(world):
    b, env_url = world["b"], world["url"]
    with db.tx() as conn:
        conn.execute("UPDATE knowledge_bases SET classification='Restricted' WHERE id=?", (world["kid"],))
        conn.execute("UPDATE chunks SET classification='Restricted'")
        conn.execute("""INSERT INTO model_connections(id,name,kind,base_url,max_classification,external,enabled,created_at)
            VALUES('x','cloud','ollama',?,'Internal',1,1,?)""", (env_url, db.now()))
    wf = b.get(f"/api/workflows/{world['wid']}").json()
    g = wf["version"]["graph"]
    for n in g["nodes"]:
        if n["type"] == "llm":
            n["data"]["connection"] = "cloud"
    v = b.post(f"/api/workflows/{world['wid']}/versions", json={"graph": g, "base_version": wf["workflow"]["latest_version"]}).json()
    assert v["valid"], v
    world["script"].requests.clear()
    run = run_to_end(b, b.post(f"/api/workflows/{world['wid']}/test", json={"question": "10-P-101A pressure"}).json()["run_id"])
    assert run["status"] == "failed" and "Restricted content may not be sent" in run["error"]
    assert not [r for r in world["script"].requests if r["path"] == "/api/chat"], "no bytes may be sent"


def test_cancel_discards_late_response(world):
    b = world["b"]
    world["script"].answers = [GOOD.format(ref=1)]
    rid = b.post(f"/api/workflows/{world['wid']}/test", json={"question": "10-P-101A pressure"}).json()["run_id"]
    import threading
    world["script"].delay_s = 1.0
    t = threading.Thread(target=drain); t.start()
    time.sleep(0.4)
    assert b.post(f"/api/runs/{rid}/cancel").status_code == 200
    t.join()
    run = b.get(f"/api/runs/{rid}").json()
    assert run["status"] == "cancelled" and run["answer"] is None


def test_missing_model_is_explicit(world):
    b = world["b"]
    with db.tx() as conn:
        db.set_setting(conn, "generation_model", "missing-model")
    wf = b.get(f"/api/workflows/{world['wid']}").json()
    g = wf["version"]["graph"]
    for n in g["nodes"]:
        if n["type"] == "llm":
            n["data"]["model"] = ""
    b.post(f"/api/workflows/{world['wid']}/versions", json={"graph": g, "base_version": wf["workflow"]["latest_version"]})
    run = run_to_end(b, b.post(f"/api/workflows/{world['wid']}/test", json={"question": "10-P-101A"}).json()["run_id"])
    assert run["status"] == "failed" and "ollama pull missing-model" in run["error"]


def test_process_description_scope_excludes_interlock(world):
    b = world["b"]
    world["script"].answers = ["The answer was not found in the accessible sources."]
    run = run_to_end(b, b.post(f"/api/workflows/{world['wid']}/test",
                                json={"question": "Describe the process flow of the unit"}).json()["run_id"])
    ev = [n for n in run["nodes"] if n["node_type"] == "retrieval"][0]["detail"]["evidence"]
    assert ev and {e["section_type"] for e in ev} == {"process_description"}


# ------------------------------------------------------------------ review → dataset → approvals
def _awaiting(world, question="What is the design discharge pressure of 10-C-101?"):
    world["script"].answers = [SWAPPED.format(ref=1)]
    v = client_for("viewer")
    pub = world["b"].post(f"/api/projects/{world['pid']}/assistants",
                          json={"workflow_id": world["wid"], "name": "Manual bot", "slug": "manual-bot"}).json()
    run = run_to_end(v, v.post(f"/api/assistants/{pub['id']}/ask", json={"question": question}).json()["run_id"])
    assert run["status"] == "awaiting_review"
    return v, run, pub


def test_human_review_correction_and_dataset_two_person_rule(world):
    v, run, _ = _awaiting(world)
    assert client_for("viewer").post(f"/api/runs/{run['id']}/feedback", json={"rating": 1}).status_code == 200
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM dataset_candidates").fetchone()[0] == 0, "thumbs-up is not training data"
    r = client_for("trainer")        # trainer also holds Reviewer
    items = r.get("/api/review/items").json()
    assert len(items) == 1
    item = r.get(f"/api/review/items/{items[0]['id']}").json()
    assert item["item"]["draft"] and item["item"]["findings"]["issues"]
    ref = next(e["ref"] for e in item["item"]["evidence"] if "10-C-101" in e["text"] and "120" in e["text"])
    bad = r.post(f"/api/review/items/{items[0]['id']}/decide", json={"decision": "correct", "answer": "No citation here"})
    assert bad.status_code == 400
    corrected = f"The design reference temperature of 10-C-101 is 120 °C [{ref}]; the sources give no discharge pressure for it."
    chk = r.post(f"/api/review/items/{items[0]['id']}/check", json={"decision": "correct", "answer": corrected}).json()
    assert isinstance(chk["issues"], list)
    d = r.post(f"/api/review/items/{items[0]['id']}/decide", json={"decision": "correct", "answer": corrected}).json()
    assert d["dataset_candidate"]
    # user now sees the engineer-approved answer
    hist = v.get("/api/me/history").json()
    assert hist[0]["answer"]["answer"] == corrected and hist[0]["answer"]["review"]["verdict"] == "engineer_corrected"
    # decisions are immutable
    with db.tx() as conn, pytest.raises(Exception):
        conn.execute("UPDATE review_decisions SET answer='x'")
    # the same person cannot approve their own correction for training
    t = client_for("trainer")
    same = t.post(f"/api/dataset/candidates/{d['dataset_candidate']}/decision", json={"decision": "include"})
    assert same.status_code == 400 and "two_person_rule" in same.text
    t2 = client_for("trainer2")
    assert t2.post(f"/api/dataset/candidates/{d['dataset_candidate']}/decision", json={"decision": "include"}).status_code == 200


def _approved_examples(world, n=3):
    """Create n approved candidates in distinct groups (distinct documents) using the real review flow."""
    b = world["b"]
    cids = []
    for i in range(n):
        if i > 0:
            text = (SAMPLES / "fictional-unit-manual.md").read_text().replace("10-P-101A", f"10-P-10{i}B") \
                .replace("Cedar", f"Cedar{i}")
            up = b.post(f"/api/knowledge-bases/{world['kid']}/documents",
                        files={"file": (f"manual{i}.md", text.encode(), "text/markdown")})
            drain(); b.post(f"/api/revisions/{up.json()['revision_id']}/confirm")
    b.post(f"/api/knowledge-bases/{world['kid']}/index"); drain()
    reviewer = client_for("reviewer")
    for i in range(n):
        tag = "10-P-101A" if i == 0 else f"10-P-10{i}B"
        world["script"].answers = [f"The discharge pressure of {tag} is 5 bar [1]."]
        v = client_for("viewer")
        pub = world["b"].post(f"/api/projects/{world['pid']}/assistants",
                              json={"workflow_id": world["wid"], "name": "Bot", "slug": "manual-bot"}).json()
        run = run_to_end(v, v.post(f"/api/assistants/{pub['id']}/ask",
                                   json={"question": f"Design discharge pressure of {tag} ({i})?"}).json()["run_id"])
        assert run["status"] == "awaiting_review", run
        item = reviewer.get("/api/review/items").json()[0]
        full = reviewer.get(f"/api/review/items/{item['id']}").json()["item"]
        ref = next(e["ref"] for e in full["evidence"] if tag in e["text"] and "5 bar" in e["text"])
        d = reviewer.post(f"/api/review/items/{item['id']}/decide",
                          json={"decision": "correct", "answer": f"The design discharge pressure of {tag} is 5 bar [{ref}]."}).json()
        cids.append(d["dataset_candidate"])
    t = client_for("trainer")
    for c in cids:
        assert t.post(f"/api/dataset/candidates/{c}/decision", json={"decision": "include"}).status_code == 200
    return cids


def test_dataset_version_splits_export_and_leakage(world):
    cids = _approved_examples(world, 3)
    t = client_for("trainer")
    data = t.get(f"/api/projects/{world['pid']}/dataset/candidates").json()
    groups = sorted({c["group_id"] for c in data["candidates"]})
    assert len(groups) == 3
    split = {groups[0]: "train", groups[1]: "validation", groups[2]: "heldout"}
    bad = t.post(f"/api/projects/{world['pid']}/dataset/versions", json={"name": "d", "version": "1",
                                                                           "split_by_group": {groups[0]: "train"}})
    assert bad.status_code == 400 and "explicit_group_split_required" in bad.text
    only_train = t.post(f"/api/projects/{world['pid']}/dataset/versions",
                        json={"name": "d", "version": "0", "split_by_group": {g: "train" for g in groups}})
    assert only_train.status_code == 400 and "split_minimums" in only_train.text
    v = t.post(f"/api/projects/{world['pid']}/dataset/versions", json={"name": "d", "version": "1", "split_by_group": split})
    assert v.status_code == 201, v.text
    vid = v.json()["id"]
    from nexagent.datasets import heldout_set, training_export
    with db.read() as conn:
        exp = training_export(conn, vid)
        held = heldout_set(conn, vid)
    assert len(exp["train"]) == 1 and len(exp["validation"]) == 1, "validation must be exported for the trainer"
    assert exp["dataset_sha256"] == v.json()["sha256"] and exp["classification"] == "Internal"
    held_q = {h["question"] for h in held}
    assert not held_q & {r["question"] for r in exp["train"] + exp["validation"]}, "held-out leaked into training"
    with db.tx() as conn, pytest.raises(Exception):
        conn.execute("UPDATE dataset_versions SET manifest_json='{}' WHERE id=?", (vid,))
    # editing an approved example invalidates its approval for the next version
    t.put(f"/api/dataset/candidates/{cids[0]}", json={"question": "edited?", "answer": "edited answer [1]"})
    v2 = t.post(f"/api/projects/{world['pid']}/dataset/versions", json={"name": "d", "version": "2", "split_by_group": split})
    assert v2.status_code == 400 or any(e["reason"] == "approval_content_changed" and e["candidate_id"] == cids[0]
                                        for e in v2.json()["exclusions"])


def test_document_deletion_restricts_dataset(world):
    _approved_examples(world, 3)
    t = client_for("trainer")
    groups = sorted({c["group_id"] for c in t.get(f"/api/projects/{world['pid']}/dataset/candidates").json()["candidates"]})
    vid = t.post(f"/api/projects/{world['pid']}/dataset/versions",
                 json={"name": "d", "version": "1", "split_by_group": dict(zip(groups, ["train", "validation", "heldout"]))}).json()["id"]
    r = world["b"].delete(f"/api/documents/{world['doc']}").json()
    assert vid in r["restricted_dataset_versions"]
    from nexagent.datasets import training_export
    with db.read() as conn, pytest.raises(ValueError):
        training_export(conn, vid)


# ------------------------------------------------------------------ training coordination (no GPU needed)
def test_training_requires_second_person_and_reports_blocked_preflight(world):
    _approved_examples(world, 3)
    t, t2 = client_for("trainer"), client_for("trainer2")
    groups = sorted({c["group_id"] for c in t.get(f"/api/projects/{world['pid']}/dataset/candidates").json()["candidates"]})
    vid = t.post(f"/api/projects/{world['pid']}/dataset/versions",
                 json={"name": "d", "version": "1", "split_by_group": dict(zip(groups, ["train", "validation", "heldout"]))}).json()["id"]
    mdir = world["settings"].models_dir / "empty-model"
    mdir.mkdir()
    (mdir / "config.json").write_text(json.dumps({"model_type": "llama"}))
    a = client_for("admin")
    assert a.post("/api/training/base-models", json={"name": "etc-model", "path": "/etc", "license": "none"}).status_code == 400
    mid = a.post("/api/training/base-models", json={"name": "empty", "path": str(mdir), "license": "test"}).json()["id"]
    assert t.post(f"/api/projects/{world['pid']}/training/jobs",
                  json={"dataset_version_id": vid, "base_model_id": mid, "recipe": "run-any-code"}).status_code == 400
    jid = t.post(f"/api/projects/{world['pid']}/training/jobs",
                 json={"dataset_version_id": vid, "base_model_id": mid, "recipe": "lora-sft"}).json()["id"]
    assert t.post(f"/api/training/jobs/{jid}/approve").status_code == 403
    assert t2.post(f"/api/training/jobs/{jid}/approve").status_code == 200
    drain()
    job = t.get(f"/api/training/jobs/{jid}").json()["job"]
    assert job["status"] == "blocked"
    failed = {c["name"] for c in job["preflight"]["checks"] if c["blocking"] and not c["ok"]}
    assert failed, job["preflight"]
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM training_metrics").fetchone()[0] == 0, "no simulated metrics"


# ------------------------------------------------------------------ publishing & API keys
def test_publish_rollback_and_api_keys(world):
    b = world["b"]
    pub = b.post(f"/api/projects/{world['pid']}/assistants",
                 json={"workflow_id": world["wid"], "name": "Manual bot", "slug": "manual-bot"}).json()
    g = b.get(f"/api/workflows/{world['wid']}").json()["version"]["graph"]
    b.post(f"/api/workflows/{world['wid']}/versions", json={"graph": g, "base_version": 1})
    pub2 = b.post(f"/api/projects/{world['pid']}/assistants",
                  json={"workflow_id": world["wid"], "name": "Manual bot", "slug": "manual-bot"}).json()
    assert pub2["version_no"] == 2
    assert b.post(f"/api/assistants/{pub['id']}/rollback").status_code == 200
    a = [x for x in b.get(f"/api/projects/{world['pid']}/assistants").json() if x["id"] == pub["id"]][0]
    assert a["version_no"] == 1
    key = b.post(f"/api/assistants/{pub['id']}/keys", json={"name": "dcs-portal"}).json()
    from fastapi.testclient import TestClient
    from nexagent.main import app
    api = TestClient(app, base_url=ORIGIN, headers={"Authorization": "Bearer " + key["key"]})
    world["script"].answers = ["The answer was not found in the accessible sources."]
    r = api.post("/api/v1/assistants/manual-bot/ask", json={"question": "10-P-101A pressure"})
    assert r.status_code == 200
    drain()
    assert api.get(f"/api/v1/runs/{r.json()['run_id']}").json()["status"] == "not_found"
    with db.read() as conn:
        stored = conn.execute("SELECT verifier FROM api_keys").fetchone()[0]
    assert key["key"].split("_", 2)[2] not in stored
    b.post(f"/api/keys/{key['id']}/revoke")
    assert api.get(f"/api/v1/runs/{r.json()['run_id']}").status_code == 401


def test_audit_is_append_only(world):
    with db.tx() as conn, pytest.raises(Exception):
        conn.execute("DELETE FROM audit_events")
    log = client_for("admin").get("/api/audit").json()
    assert any(e["action"] == "kb.index_activated" for e in log)


def test_backup_and_restore(world, tmp_path, monkeypatch):
    from nexagent import cli, config
    cli.backup(str(tmp_path / "bk"))
    archive = next((tmp_path / "bk").glob("*.tar.gz"))
    s = config.settings()
    new = config.build_settings()
    new.data_dir = tmp_path / "restored"
    config.override_settings(new)
    try:
        cli.restore(str(archive))
        import sqlite3
        with sqlite3.connect(new.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 8
            assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] > 0
    finally:
        config.override_settings(s)


def test_training_operator_without_kb_access_cannot_see_examples(world):
    _approved_examples(world, 1)
    world["b"].put(f"/api/knowledge-bases/{world['kid']}/grants", json={"user_id": world["ids"]["trainer2"], "can_read": False})
    t2 = client_for("trainer2")
    assert t2.get(f"/api/projects/{world['pid']}/dataset/candidates").json()["candidates"] == []
