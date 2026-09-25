import importlib.util
import json
from pathlib import Path

import pytest

from conftest import client_for, make_user
from nexagent import db
from nexagent.llm import ModelUnavailable, resolve_model
from nexagent.training.coordinator import rollback, score, token_f1

ROOT = Path(__file__).resolve().parents[2]


def _golden():
    spec = importlib.util.spec_from_file_location("run_golden", ROOT / "evaluations" / "run_golden.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_golden_suite_composition():
    cases = [json.loads(l) for l in (ROOT / "evaluations" / "golden_cases.jsonl").read_text().splitlines() if l.strip()]
    assert len(cases) == 50 and len({c["id"] for c in cases}) == 50
    cats = {c["category"] for c in cases}
    langs = {c["language"] for c in cases}
    assert {"answerable", "unanswerable", "adversarial"} <= cats and {"en", "hi", "hinglish"} <= langs


def test_golden_scoring_rules():
    g = _golden()
    ans = {"expect": "answer", "must_contain": ["355 °C"], "must_not_contain": []}
    assert g.score(ans, {"status": "completed", "answer": {"answer": "It is 355 deg C [1]."}}) == "PASS"
    assert g.score(ans, {"status": "completed", "answer": {"answer": "It is 380 °C [1]."}}) == "FAIL"
    assert g.score(ans, {"status": "awaiting_review", "answer": {"answer": ""}}) == "ROUTED"
    assert g.score(ans, {"status": "not_found", "answer": {"answer": "The answer was not found"}}) == "ABSTAINED"
    nf = {"expect": "not_found", "must_contain": [], "must_not_contain": []}
    assert g.score(nf, {"status": "completed", "answer": {"answer": "It is 5 bar"}}) == "FAIL"
    safe = {"expect": "safe", "must_contain": [], "must_not_contain": ["999"]}
    assert g.score(safe, {"status": "completed", "answer": {"answer": "The setpoint is 999 °C"}}) == "FAIL"


def test_model_eval_scoring_penalises_abstention():
    rows = [{"id": "1", "question": "q", "answer": "The design pressure of 10-P-101A is 5 bar [1].",
             "evidence": [{"ref": 1, "chunk_id": "c", "document_id": "d", "revision_no": 1, "filename": "f",
                           "section": "s", "section_type": "equipment_spec", "location": "l",
                           "text": "Equipment: 10-P-101A | Property: Design pressure | Value: 5 bar | Condition: Design"}]}]
    abstain = score(rows, [{"id": "1", "output": "The answer was not found in the accessible sources.", "latency_ms": 5}])
    good = score(rows, [{"id": "1", "output": "The design pressure of 10-P-101A is 5 bar [1].", "latency_ms": 5}])
    assert abstain["useful_answer_rate_answerable"] == 0 and abstain["abstention_rate_answerable"] == 1
    assert good["useful_answer_rate_answerable"] == 1 and good["numeric_tag_preservation"] == 1
    assert token_f1("a b c", "a b c") == 1.0


def test_alias_pinning_and_rollback(env):
    import sqlite3
    raw = sqlite3.connect(db.settings().db_path)          # fixture rows without the full training chain
    raw.execute("PRAGMA foreign_keys=OFF")
    with raw as conn:
        for i, name in (("d1", "nexagent-cdu:1"), ("d2", "nexagent-cdu:2")):
            conn.execute("INSERT INTO deployments VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (i, "cdu", "a", "e", name, "active" if i == "d2" else "superseded", "u", None, db.now(), db.now()))
        conn.execute("INSERT INTO model_aliases VALUES('cdu','d2','d1',?)", (db.now(),))
    raw.close()
    with db.read() as conn:
        assert resolve_model(conn, "promoted:cdu") == "nexagent-cdu:2"
        assert resolve_model(conn, "qwen2.5:7b") == "qwen2.5:7b"
    with db.tx() as conn:
        rollback(conn, "cdu", "u")
    with db.read() as conn:
        assert resolve_model(conn, "promoted:cdu") == "nexagent-cdu:1"
        with pytest.raises(ModelUnavailable):
            resolve_model(conn, "promoted:unknown")
    with db.tx() as conn, pytest.raises(ValueError):
        rollback(conn, "cdu", "u")          # only one step of history is kept after a rollback


def test_promotion_permission_is_separate(env):
    make_user("operator1", ["TrainingOperator", "Admin"])
    c = client_for("operator1")
    r = c.post("/api/deployments", json={"evaluation_id": "x", "alias": "cdu"})
    assert r.status_code == 403 and "promotion permission" in r.text


def test_run_interrupted_by_restart_is_marked_failed(env):
    from nexagent import jobs
    with db.tx() as conn:
        conn.execute("""INSERT INTO runs(id,project_id,workflow_version_id,kind,question,status,created_at)
            VALUES('r1','p','v','test','q','running',?)""", (db.now(),))
        conn.execute("""INSERT INTO jobs(id,kind,payload,queue,status,attempts,max_attempts,lease_until,created_at,updated_at)
            VALUES('j1','run.execute','{"run_id":"r1"}','default','leased',1,1,?,?,?)""", (db.now() - 1, db.now(), db.now()))
    jobs.run_one()
    with db.read() as conn:
        run = db.one(conn, "SELECT status,error FROM runs WHERE id='r1'")
    assert run["status"] == "failed" and "restarted" in run["error"]


def test_qwen3_think_blocks_are_removed():
    from nexagent.llm import strip_thinking
    assert strip_thinking("<think>private reasoning 999 °C</think>\nThe answer [1].") == "The answer [1]."
    assert strip_thinking("reasoning without opening tag</think>Answer") == "Answer"
    assert strip_thinking("Plain answer") == "Plain answer"


def test_qwen3_embedding_query_instruction():
    from nexagent.llm import query_for_embedding
    assert query_for_embedding("qwen3-embedding:8b", "pump pressure?").startswith("Instruct:")
    assert query_for_embedding("nomic-embed-text", "pump pressure?") == "pump pressure?"


def test_headings_are_not_claims_but_cannot_invent_tags():
    from nexagent.validators import validate_answer
    ev = [{"ref": 1, "text": "Equipment: 10-P-101A | Property: Design pressure | Value: 5 bar | Condition: Design",
           "section_type": "equipment_spec", "document_id": "d", "revision_no": 1, "equipment_tags": ["10-P-101A"]}]
    ok = "### Pump 10-P-101A design data\nThe design pressure of 10-P-101A is 5 bar [1]."
    assert validate_answer("q", ok, ev) == []
    bad = "### Pump 10-P-999 data\nThe design pressure of 10-P-101A is 5 bar [1]."
    assert {i.code for i in validate_answer("q", bad, ev)} == {"unknown_equipment_tag"}


def test_ollama_requests_carry_context_window_and_keep_alive(env):
    from nexagent.llm import Gateway
    env["script"].answers = ["<think>x</think>Hello"]
    res = Gateway().chat([{"role": "user", "content": "hi"}], "test-gen", "Internal")
    body = env["script"].requests[-1]["body"]
    assert res.text == "Hello"
    assert body["options"]["num_ctx"] == env["settings"].num_ctx and body["keep_alive"]
