import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nexagent import config, db, jobs  # noqa: E402
from nexagent.security import hash_password  # noqa: E402
import fake_ollama  # noqa: E402

ORIGIN = "http://127.0.0.1:8600"
PASSWORD = "correct-horse-battery-9"
SAMPLES = Path(__file__).resolve().parents[2] / "samples"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    script = fake_ollama.Script()
    server = fake_ollama.start(script)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    for k in list(os.environ):
        if k.startswith("NEXAGENT_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("NEXAGENT_DATA_DIR", str(tmp_path / "data"))
    s = config.build_settings()
    s.ollama_url = url
    s.workers = 0
    s.origin = ORIGIN
    s.default_generation_model = "test-gen"
    s.default_review_model = "test-gen"
    s.default_embedding_model = "test-embed"
    s.models_dir = tmp_path / "models"
    s.models_dir.mkdir()
    s.train_python = os.environ.get("TEST_TRAIN_PYTHON", "")
    config.override_settings(s)
    from nexagent.main import init_state
    init_state()
    yield {"script": script, "url": url, "tmp": tmp_path, "settings": s}
    server.shutdown()


def make_user(username, roles, perms=()):
    uid = db.new_id()
    with db.tx() as conn:
        conn.execute("INSERT INTO users(id,username,display_name,password_hash,active,created_at) VALUES(?,?,?,?,1,?)",
                     (uid, username, username.title(), hash_password(PASSWORD), db.now()))
        conn.executemany("INSERT INTO user_roles VALUES(?,?)", [(uid, r) for r in roles])
        conn.executemany("INSERT INTO user_permissions VALUES(?,?)", [(uid, p) for p in perms])
    return uid


def client_for(username, password=PASSWORD):
    from fastapi.testclient import TestClient
    from nexagent.main import app
    c = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


def drain(queue="default", limit=200):
    n = 0
    while jobs.run_one(queue) and n < limit:
        n += 1
    return n
