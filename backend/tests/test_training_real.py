"""REAL training run (LoRA on a tiny, locally generated, random-weight test model).

Skipped unless TEST_TRAIN_PYTHON points at a Python with torch+transformers+peft and
TEST_TINY_MODEL points at the fixture built by scripts/make_tiny_test_model.py.
Proves: preflight → approved launch → real metrics → adapter + hashes → evaluation → promotion
attempt (honest failure when Ollama is absent) → cancellation.
"""
import json
import os
import shutil
import threading
import time

import pytest

from conftest import drain
from test_api_flow import _approved_examples, world  # noqa: F401  (fixture reuse)
from conftest import client_for
from nexagent import db

pytestmark = pytest.mark.skipif(not (os.environ.get("TEST_TRAIN_PYTHON") and os.environ.get("TEST_TINY_MODEL")),
                                reason="needs TEST_TRAIN_PYTHON and TEST_TINY_MODEL")


def _setup(world, recipe="lora-smoke-test", params=None):
    _approved_examples(world, 3)
    t, t2 = client_for("trainer"), client_for("trainer2")
    groups = sorted({c["group_id"] for c in t.get(f"/api/projects/{world['pid']}/dataset/candidates").json()["candidates"]})
    vid = t.post(f"/api/projects/{world['pid']}/dataset/versions",
                 json={"name": "d", "version": "1",
                       "split_by_group": dict(zip(groups, ["train", "validation", "heldout"]))}).json()["id"]
    dest = world["settings"].models_dir / "tiny"
    shutil.copytree(os.environ["TEST_TINY_MODEL"], dest)
    mid = client_for("admin").post("/api/training/base-models",
                                   json={"name": "tiny", "path": str(dest), "license": "test fixture"}).json()["id"]
    jid = t.post(f"/api/projects/{world['pid']}/training/jobs",
                 json={"dataset_version_id": vid, "base_model_id": mid, "recipe": recipe,
                       "params": params or {}, "seed": 7}).json()["id"]
    assert t2.post(f"/api/training/jobs/{jid}/approve").status_code == 200
    drain()                                    # preflight
    return t, jid


def test_real_lora_training_evaluation_and_promotion(world):
    t, jid = _setup(world, params={"epochs": 3})
    job = t.get(f"/api/training/jobs/{jid}").json()["job"]
    assert job["status"] == "queued", job["preflight"]
    drain("training")                          # runs the real runner process to completion
    d = t.get(f"/api/training/jobs/{jid}").json()
    assert d["job"]["status"] == "completed", d["job"]["error"]
    assert d["metrics"] and all(m["loss"] is None or m["loss"] > 0 for m in d["metrics"])
    assert d["job"]["software"]["peft"] and d["job"]["hardware"]["cpu_count"]
    art = d["artifacts"][0]
    assert "adapter_model.safetensors" in art["files"] and art["classification"] == "Internal"
    job_dir = world["settings"].training_dir / jid
    assert not any("heldout" in p.name for p in job_dir.iterdir()), "held-out data must not be in the job folder"
    train_q = [json.loads(l)["question"] for l in (job_dir / "train.jsonl").read_text().splitlines()]
    eid = t.post(f"/api/artifacts/{art['id']}/evaluate").json()["id"]
    drain("training")
    ev = [e for e in t.get(f"/api/training/jobs/{jid}").json()["evaluations"] if e["id"] == eid][0]
    assert ev["status"] == "completed", ev
    res = ev["results"]
    assert set(res["base"]) == set(res["candidate"]) and res["base"]["examples"] == 1
    assert "deterministic" in res["method"]
    assert not set(train_q) & {s["question"] for s in res["samples"]}
    # promotion: requester cannot promote; promoter can; export fails honestly without an Ollama CLI
    with db.tx() as conn:
        conn.execute("INSERT INTO user_permissions VALUES((SELECT id FROM users WHERE username='trainer'),'model.promote')")
    assert client_for("trainer").post("/api/deployments", json={"evaluation_id": eid, "alias": "cdu"}).status_code == 403
    world["settings"].ollama_cli = "/nonexistent/ollama"
    p = client_for("promoter").post("/api/deployments", json={"evaluation_id": eid, "alias": "cdu"})
    assert p.status_code == 201
    drain("training")
    dep = client_for("promoter").get("/api/deployments").json()["deployments"][0]
    assert dep["status"] == "export_failed" and "ollama" in dep["error"].lower()
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM model_aliases").fetchone()[0] == 0, "nothing switched"


def test_real_training_cancellation(world, monkeypatch):
    monkeypatch.setenv("NX_TEST_STEP_DELAY_S", "0.5")
    t, jid = _setup(world, params={"epochs": 20, "max_len": 1024})
    th = threading.Thread(target=drain, args=("training",))
    th.start()
    for _ in range(300):
        time.sleep(0.2)
        if t.get(f"/api/training/jobs/{jid}").json()["metrics"]:
            break
    assert t.post(f"/api/training/jobs/{jid}/cancel").json()["status"] == "cancelling"
    th.join(timeout=300)
    job = t.get(f"/api/training/jobs/{jid}").json()["job"]
    assert job["status"] == "cancelled", job
