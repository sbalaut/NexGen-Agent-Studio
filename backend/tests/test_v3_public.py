"""v0.3: public providers, personal keys, MCP, multi-agent, GraphRAG, public mode."""
import ipaddress
import json
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import fake_providers
from conftest import ORIGIN, PASSWORD, SAMPLES, client_for, drain, make_user
from nexagent import db, llm
from nexagent.llm import Gateway, ModelUnavailable
from nexagent.validators import validate_answer

HERE = Path(__file__).resolve().parent
MCP_SERVER = str(HERE / "mcp_demo_server.py")


# ------------------------------------------------------------------ helpers
@pytest.fixture()
def prov(env):
    script = fake_providers.Script()
    server = fake_providers.start(script)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield {"script": script, "url": url}
    server.shutdown()


def shared_connection(name, kind, url, key="sk-test-key", ceiling="Internal"):
    with db.tx() as conn:
        conn.execute("""INSERT INTO model_connections(id,name,kind,base_url,max_classification,external,enabled,api_key_enc,created_at)
            VALUES(?,?,?,?,?,1,1,?,?)""", (db.new_id(), name, kind, url, ceiling, llm.encrypt_secret(key), db.now()))


def project_with_kb(client, require_review=None, graph_mode="off", sample="fictional-cedar-unit-manual-rev-b.md"):
    body = {"name": "P", "classification_floor": "Public"}
    if require_review is not None:
        body["require_review"] = require_review
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    kid = client.post(f"/api/projects/{pid}/knowledge-bases",
                      json={"name": "Manual", "classification": "Internal", "graph_mode": graph_mode}).json()["id"]
    up = client.post(f"/api/knowledge-bases/{kid}/documents",
                     files={"file": (sample, (SAMPLES / sample).read_bytes(), "text/markdown")})
    drain()
    assert client.post(f"/api/revisions/{up.json()['revision_id']}/confirm").status_code == 200
    assert client.post(f"/api/knowledge-bases/{kid}/index").status_code == 200
    drain()
    return pid, kid


def save_graph(client, pid, graph):
    wf = client.post(f"/api/projects/{pid}/workflows", json={"name": "W", "kb_ids": []}).json()
    r = client.post(f"/api/workflows/{wf['id']}/versions", json={"graph": graph, "base_version": 1}).json()
    return wf["id"], r


def N(i, t, x=0, **data):
    return {"id": i, "type": t, "position": {"x": x, "y": 0}, "data": data}


def E(i, s, sh, t, th):
    return {"id": i, "source": s, "sourceHandle": sh, "target": t, "targetHandle": th}


def run(client, wid, q):
    rid = client.post(f"/api/workflows/{wid}/test", json={"question": q}).json()["run_id"]
    drain()
    return client.get(f"/api/runs/{rid}").json()


# ------------------------------------------------------------------ providers
@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
def test_provider_chat_tools_and_auth(env, prov, kind):
    shared_connection("p", kind, prov["url"])
    gw = Gateway("p")
    prov["script"].queue = ["Plain answer."]
    assert gw.chat([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], "fake-model", "Internal").text \
        == "Plain answer."
    req = prov["script"].requests[-1]
    if kind == "anthropic":
        assert req["headers"]["x-api-key"] == "sk-test-key" and req["body"]["system"] == "sys"
        assert req["headers"]["anthropic-version"]
    elif kind == "gemini":
        assert req["headers"]["x-goog-api-key"] == "sk-test-key"
        assert req["body"]["systemInstruction"]["parts"][0]["text"] == "sys"
    else:
        assert req["headers"]["Authorization"] == "Bearer sk-test-key"
    tool = {"name": "add", "description": "Add", "parameters": {"type": "object", "title": "x", "additionalProperties": False,
                                                                 "properties": {"a": {"type": ["integer", "null"]}}}}
    prov["script"].queue = [{"tool_calls": [{"name": "add", "arguments": {"a": 2}}]}]
    res = gw.chat([{"role": "user", "content": "add"}], "fake-model", "Internal", tools=[tool])
    assert res.tool_calls and res.tool_calls[0]["name"] == "add" and res.tool_calls[0]["arguments"] == {"a": 2}
    body = prov["script"].requests[-1]["body"]
    if kind == "gemini":
        params = body["tools"][0]["functionDeclarations"][0]["parameters"]
        assert "additionalProperties" not in params and params["properties"]["a"]["type"] == "integer"
    # tool result round trip
    prov["script"].queue = ["Done: 2"]
    msgs = [{"role": "user", "content": "add"},
            {"role": "assistant", "content": "", "tool_calls": res.tool_calls},
            {"role": "tool", "tool_call_id": res.tool_calls[0]["id"], "name": "add", "content": "2"}]
    assert gw.chat(msgs, "fake-model", "Internal", tools=[tool]).text == "Done: 2"
    body = prov["script"].requests[-1]["body"]
    if kind == "anthropic":
        assert body["messages"][-1]["content"][0]["type"] == "tool_result"
    elif kind == "gemini":
        assert "functionResponse" in body["contents"][-1]["parts"][0]
    else:
        assert body["messages"][-1]["role"] == "tool"
    if kind != "anthropic":
        assert len(gw.embed(["a", "b"], "fake-embed", "Internal")) == 2
    else:
        with pytest.raises(ModelUnavailable, match="no embedding API"):
            gw.embed(["a"], "x", "Internal")
    assert "fake-model" in gw.list_models()


def test_wrong_key_and_classification_ceiling(env, prov):
    shared_connection("bad", "openai", prov["url"], key="wrong")
    with pytest.raises(ModelUnavailable, match="rejected the API key"):
        Gateway("bad").chat([{"role": "user", "content": "x"}], "m", "Internal")
    shared_connection("pub", "anthropic", prov["url"], ceiling="Public")
    before = len(prov["script"].requests)
    with pytest.raises(llm.GatewayBlocked):
        Gateway("pub").chat([{"role": "user", "content": "x"}], "m", "Internal")
    assert len(prov["script"].requests) == before, "nothing may be sent"


# ------------------------------------------------------------------ personal keys (public mode)
def test_personal_keys_ssrf_and_privacy(env, monkeypatch):
    env["settings"].public_mode = True
    real = llm._resolve_ips
    monkeypatch.setattr(llm, "_resolve_ips", lambda host, port: [ipaddress.ip_address("93.184.216.34")]
                        if host.startswith("api.") or host.endswith("googleapis.com") else real(host, port))
    make_user("alice", ["Builder"]); make_user("bob", ["Builder"])
    a = client_for("alice")
    r = a.post("/api/me/connections", json={"name": "local", "provider": "openai-compatible", "api_key": "sk-12345678",
                                             "base_url": "http://127.0.0.1:11434"})
    assert r.status_code == 400 and "https" in r.text
    r = a.post("/api/me/connections", json={"name": "internal", "provider": "openai-compatible", "api_key": "sk-12345678",
                                             "base_url": "https://localhost"})
    assert r.status_code == 400 and "non-public" in r.text
    assert a.post("/api/me/connections", json={"name": "my-openai", "provider": "openai", "api_key": "sk-secret-123"}).status_code == 201
    mine = a.get("/api/me/connections").json()["mine"]
    assert mine[0]["name"] == "my-openai" and "sk-secret" not in json.dumps(mine)
    assert client_for("bob").get("/api/me/connections").json()["mine"] == []
    with db.read() as conn:
        assert "sk-secret" not in json.dumps(db.all_rows(conn, "SELECT * FROM model_connections"))
    r = a.post("/api/me/connections", json={"name": "x", "provider": "anthropic", "api_key": "sk-12345678",
                                             "max_classification": "Restricted"})
    assert r.status_code == 422


# ------------------------------------------------------------------ MCP
def test_mcp_stdio_admin_only_tools_start_disabled(env):
    make_user("admin1", ["Admin", "Builder"]); make_user("carol", ["Builder"])
    ad = client_for("admin1")
    r = ad.post("/api/mcp/servers", json={"name": "demo", "transport": "stdio", "command": sys.executable,
                                          "args": [MCP_SERVER], "max_classification": "Internal", "shared": True})
    assert r.status_code == 201, r.text
    servers = ad.get("/api/mcp/servers").json()["servers"]
    tools = {t["name"]: t for t in servers[0]["tools"]}
    assert set(tools) == {"add", "plant_status"} and not any(t["enabled"] for t in tools.values())
    assert client_for("carol").get("/api/mcp/servers").json()["servers"][0]["tools"] == [], "disabled tools hidden"
    assert ad.put(f"/api/mcp/servers/{servers[0]['id']}/tools/add", json={"enabled": True}).status_code == 200
    visible = client_for("carol").get("/api/mcp/servers").json()["servers"][0]
    assert [t["name"] for t in visible["tools"]] == ["add"] and visible["command"] is None
    env["settings"].public_mode = True
    r = client_for("carol").post("/api/mcp/servers", json={"name": "xx", "transport": "stdio", "command": "/bin/sh"})
    assert r.status_code == 403
    r = client_for("carol").post("/api/mcp/servers", json={"name": "yy", "transport": "http", "url": "http://127.0.0.1:9/mcp"})
    assert r.status_code == 400


def test_mcp_remote_http_real_server(env):
    port = _free_port()
    proc = subprocess.Popen([sys.executable, MCP_SERVER, "http", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_port(port)
        make_user("admin2", ["Admin", "Builder"])
        ad = client_for("admin2")
        r = ad.post("/api/mcp/servers", json={"name": "remote", "transport": "http", "url": f"http://127.0.0.1:{port}/mcp",
                                              "shared": True})
        assert r.status_code == 201
        s = ad.get("/api/mcp/servers").json()["servers"][0]
        assert s["last_error"] is None and {t["name"] for t in s["tools"]} == {"add", "plant_status"}
        from nexagent.mcp_client import call_tool
        assert call_tool({"transport": "http", "url": f"http://127.0.0.1:{port}/mcp"}, {}, "add", {"a": 20, "b": 22}).text == "42"
    finally:
        proc.kill()


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _wait_port(port, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close(); return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


# ------------------------------------------------------------------ multi-agent
def _mcp_add_enabled(client):
    r = client.post("/api/mcp/servers", json={"name": "demo", "transport": "stdio", "command": sys.executable,
                                              "args": [MCP_SERVER], "max_classification": "Internal", "shared": True})
    sid = r.json()["id"]
    client.put(f"/api/mcp/servers/{sid}/tools/add", json={"enabled": True})
    client.put(f"/api/mcp/servers/{sid}/tools/plant_status", json={"enabled": True})
    return sid


def test_agent_uses_mcp_and_knowledge_then_direct_output(env):
    make_user("boss", ["Admin", "Builder"])
    c = client_for("boss")
    pid, kid = project_with_kb(c, require_review=False)
    sid = _mcp_add_enabled(c)
    graph = {"nodes": [N("in", "input"), N("ag", "agent", 200, name="helper", kb_ids=[kid],
                                            mcp_tools=[f"{sid}/add"], instructions="Be helpful."),
                       N("out", "final", 400)],
             "edges": [E("e1", "in", "question", "ag", "question"), E("e2", "ag", "answer", "out", "answer"),
                       E("e3", "ag", "evidence", "out", "evidence")]}
    wid, v = save_graph(c, pid, graph)
    assert v["valid"], v
    replies = [{"tool_calls": [{"name": "mcp_demo__add", "arguments": {"a": 2, "b": 3}}]},
               {"tool_calls": [{"name": "search_knowledge", "arguments": {"query": "TZH-2051 trip setpoint"}}]}]

    def responder(messages):
        if replies:
            return replies.pop(0)
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert tool_msgs[0]["content"] == "5"
        ref = next(line.split("]")[0].strip("[") for line in tool_msgs[1]["content"].splitlines()
                   if line.startswith("[") and "Interlocks" in line)
        return f"The TZH-2051 trip setpoint is > 380 °C [{ref}]. Also 2+3=5."
    env["script"].responder = responder
    r = run(c, wid, "What is the TZH-2051 trip setpoint?")
    assert r["status"] == "completed", r
    assert "380" in r["answer"]["answer"] and r["answer"]["citations"][0]["filename"].startswith("fictional-cedar")
    assert r["answer"]["review"]["verdict"] == "not_reviewed"
    trace = [n for n in r["nodes"] if n["node_type"] == "agent"][0]["detail"]["trace"]
    assert [t.get("tool") for t in trace if t["type"] == "tool"] == ["mcp_demo__add", "search_knowledge"]


def test_team_delegates_to_member(env):
    make_user("lead", ["Admin", "Builder"])
    c = client_for("lead")
    pid, kid = project_with_kb(c, require_review=False)
    team = N("tm", "team", 200, members=[{"name": "researcher", "role": "facts", "instructions": "Search.", "kb_ids": [kid],
                                          "mcp_tools": []}])
    graph = {"nodes": [N("in", "input"), team, N("out", "final", 400)],
             "edges": [E("e1", "in", "question", "tm", "question"), E("e2", "tm", "answer", "out", "answer"),
                       E("e3", "tm", "evidence", "out", "evidence")]}
    wid, v = save_graph(c, pid, graph)
    assert v["valid"], v
    calls = []

    def responder(messages):
        sys_prompt = messages[0]["content"]
        has_tool_result = any(m["role"] == "tool" for m in messages)
        if "lead a team" in sys_prompt:
            calls.append("supervisor")
            if not has_tool_result:
                return {"tool_calls": [{"name": "delegate_to_researcher", "arguments": {"task": "find the design duty of 20-F-205"}}]}
            return "Team answer: " + [m for m in messages if m["role"] == "tool"][0]["content"].split("\n", 1)[1]
        calls.append("member")
        if not has_tool_result:
            return {"tool_calls": [{"name": "search_knowledge", "arguments": {"query": "20-F-205 design duty"}}]}
        return "The design duty of 20-F-205 is 42 MW [1]."
    env["script"].responder = responder
    r = run(c, wid, "Design duty of the heater?")
    assert r["status"] == "completed" and "42 MW" in r["answer"]["answer"]
    assert calls[:3] == ["supervisor", "member", "member"]


def test_direct_output_blocked_where_review_required(env):
    make_user("eng", ["Builder"])
    c = client_for("eng")
    pid, kid = project_with_kb(c)             # plant default: review required
    graph = {"nodes": [N("in", "input"), N("ag", "agent", 200), N("out", "final", 400)],
             "edges": [E("e1", "in", "question", "ag", "question"), E("e2", "ag", "answer", "out", "answer")]}
    _, v = save_graph(c, pid, graph)
    assert not v["valid"] and any("requires Answer Review" in e for e in v["errors"])
    assert c.post("/api/projects", json={"name": "x", "require_review": False}).status_code == 403


def test_agent_mcp_call_blocked_by_classification(env):
    make_user("boss2", ["Admin", "Builder"])
    c = client_for("boss2")
    pid, kid = project_with_kb(c, require_review=False)
    r = c.post("/api/mcp/servers", json={"name": "pub", "transport": "stdio", "command": sys.executable,
                                         "args": [MCP_SERVER], "max_classification": "Public", "shared": True})
    sid = r.json()["id"]
    c.put(f"/api/mcp/servers/{sid}/tools/plant_status", json={"enabled": True})
    graph = {"nodes": [N("in", "input"), N("ag", "agent", 200, kb_ids=[kid], mcp_tools=[f"{sid}/plant_status"]),
                       N("out", "final", 400)],
             "edges": [E("e1", "in", "question", "ag", "question"), E("e2", "ag", "answer", "out", "answer")]}
    wid, v = save_graph(c, pid, graph)
    steps = [{"tool_calls": [{"name": "search_knowledge", "arguments": {"query": "20-C-206"}}]},
             {"tool_calls": [{"name": "mcp_pub__plant_status", "arguments": {"unit": "CDU"}}]}, "done"]
    env["script"].responder = lambda m: steps.pop(0) if len(steps) > 1 else steps[0]
    r = run(c, wid, "status?")
    trace = [n for n in r["nodes"] if n["node_type"] == "agent"][0]["detail"]["trace"]
    mcp_step = [t for t in trace if t.get("tool") == "mcp_pub__plant_status"][0]
    assert mcp_step["result"].startswith("BLOCKED by policy")


# ------------------------------------------------------------------ GraphRAG
def test_graphrag_rules_build_and_retrieval(env):
    make_user("graphuser", ["Builder"])
    c = client_for("graphuser")
    pid, kid = project_with_kb(c, graph_mode="rules")
    g = c.get(f"/api/knowledge-bases/{kid}/graph").json()
    names = {e["name"] for e in g["entities"]}
    assert {"20-F-205", "TZH-2051"} <= names and g["relations"] and g["communities"]
    assert "entities" in g["status"]
    sub = c.get(f"/api/knowledge-bases/{kid}/graph?q=tzh-2051").json()
    assert sub["entities"][0]["name"] == "TZH-2051" and len(sub["entities"]) > 1
    from nexagent.retrieval import retrieve
    with db.read() as conn:
        items = retrieve(conn, "What protects 20-F-205?", {kid}, top_k=6, graph="both")
    assert any("TZH-2051" in i.text for i in items)
    assert any(i.section_type == "graph_summary" for i in items)


def test_graphrag_llm_extraction(env):
    make_user("graphllm", ["Builder"])
    c = client_for("graphllm")
    env["script"].json_responder = lambda msgs: (
        {"entities": [{"name": "Desalter", "type": "equipment", "description": "Removes salt", "passages": [1]},
                      {"name": "Wash water", "type": "chemical", "description": "Water injected", "passages": [1]}],
         "relations": [{"source": "Wash water", "target": "Desalter", "relation": "injected into", "passage": 1}]}
        if "Extract a knowledge graph" in msgs[-1]["content"] else {"title": "Desalting", "summary": "Desalter and wash water."})
    pid, kid = project_with_kb(c, graph_mode="llm")
    g = c.get(f"/api/knowledge-bases/{kid}/graph?q=desalter").json()
    assert any(e["name"] == "Desalter" and e["type"] == "equipment" for e in g["entities"])
    assert any(r["relation"] == "injected into" for r in g["relations"])
    assert any(cm["title"] == "Desalting" for cm in g["communities"])


def test_summary_is_not_a_primary_source():
    ev = [{"ref": 1, "text": "TZH-2051 trips at 380 °C.", "section_type": "graph_summary", "document_id": "g",
           "revision_no": 0, "equipment_tags": []}]
    assert {i.code for i in validate_answer("q", "TZH-2051 trips above 380 °C [1].", ev)} == {"summary_not_primary_source"}


# ------------------------------------------------------------------ public mode
def test_signup_workspace_limits_and_hidden_workspaces(env):
    s = env["settings"]
    s.public_mode, s.allow_signup, s.signups_per_ip_per_day = True, True, 1
    s.features = ("mcp", "agents", "graphrag")
    from fastapi.testclient import TestClient
    from nexagent.main import app
    c = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    assert c.get("/api/config").json()["allow_signup"] is True
    r = c.post("/api/auth/signup", json={"username": "newbie", "display_name": "New", "password": PASSWORD})
    assert r.status_code == 201, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    projects = c.get("/api/projects").json()
    assert projects[0]["name"] == "My workspace" and projects[0]["require_review"] == 0
    assert c.get("/api/users/directory").json() == []
    assert c.get("/api/review/items").status_code == 404
    c2 = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    assert c2.post("/api/auth/signup", json={"username": "second", "display_name": "S", "password": PASSWORD}).status_code == 429
    s.allow_signup = False
    assert c2.post("/api/auth/signup", json={"username": "third", "display_name": "T", "password": PASSWORD}).status_code == 403


def test_run_quota(env):
    env["settings"].public_mode = True
    env["settings"].quota_runs_per_hour = 1
    make_user("quota", ["Builder"])
    c = client_for("quota")
    pid, kid = project_with_kb(c, require_review=False)
    graph = {"nodes": [N("in", "input"), N("p", "prompt", 100, template="{question}"), N("l", "llm", 200), N("o", "final", 300)],
             "edges": [E("1", "in", "question", "p", "question"), E("2", "p", "prompt", "l", "prompt"),
                       E("3", "l", "draft", "o", "answer")]}
    wid, v = save_graph(c, pid, graph)
    assert v["valid"], v
    env["script"].answers = ["Hi there."]
    assert run(c, wid, "hello")["answer"]["answer"] == "Hi there."
    assert c.post(f"/api/workflows/{wid}/test", json={"question": "again"}).status_code == 429


def test_unverified_answer_without_review_queue(env):
    env["settings"].features = ("mcp", "agents", "graphrag")
    make_user("noqueue", ["Builder"])
    c = client_for("noqueue")
    pid, kid = project_with_kb(c, sample="fictional-unit-manual.md")
    wf = c.post(f"/api/projects/{pid}/workflows", json={"name": "Q", "kb_ids": [kid]}).json()
    env["script"].answers = ["The design discharge pressure of 10-C-101 is 5 bar [1]."]
    r = run(c, wf["id"], "What is the design discharge pressure of 10-C-101?")
    assert r["status"] == "not_found" and r["answer"]["answer"] == ""
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM review_items").fetchone()[0] == 0


def test_migration_from_v3_keeps_connections(tmp_path, monkeypatch):
    from nexagent import config
    s = config.build_settings(); s.data_dir = tmp_path / "old"; s.data_dir.mkdir()
    config.override_settings(s)
    conn = sqlite3.connect(s.db_path)
    conn.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
    for i, script in enumerate(db.SCHEMA[:3], start=1):
        for stmt in db._split(script):
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_version VALUES(?)", (i,))
    conn.execute("INSERT INTO model_connections(id,name,kind,base_url,max_classification,external,enabled,created_at) "
                 "VALUES('c1','gpu','openai','http://gpu:8000','Internal',0,1,0)")
    conn.commit(); conn.close()
    assert db.migrate() == 4
    with db.read() as c:
        row = db.one(c, "SELECT * FROM model_connections WHERE id='c1'")
    assert row["name"] == "gpu" and row["owner_user_id"] is None


def test_hosted_connection_requires_explicit_model():
    from nexagent.graph import default_graph, validate_graph
    g = default_graph([], "", "", template="chat", connection="my-openai", require_review=False)
    ctx = dict(allowed_kbs=set(), connections={"my-openai"}, require_review=False)
    errs = validate_graph(g, **ctx, hosted={"my-openai"})
    assert any("choose a model" in e for e in errs)
    g2 = default_graph([], "gpt-4o-mini", "", template="chat", connection="my-openai", require_review=False)
    assert validate_graph(g2, **ctx, hosted={"my-openai"}) == []
    # Ollama connections may still fall back to the administrator's default model
    g3 = default_graph([], "", "", template="chat", connection="my-openai", require_review=False)
    assert validate_graph(g3, **ctx) == []


def test_bootstrap_admin_from_environment(env, monkeypatch, capsys):
    from nexagent import cli
    monkeypatch.setenv("NEXAGENT_BOOTSTRAP_ADMIN", "Owner")
    monkeypatch.setenv("NEXAGENT_BOOTSTRAP_ADMIN_PASSWORD", "short")
    with pytest.raises(SystemExit):
        cli.bootstrap_admin_from_env()
    monkeypatch.setenv("NEXAGENT_BOOTSTRAP_ADMIN_PASSWORD", "a-long-enough-password")
    cli.bootstrap_admin_from_env()
    with db.read() as conn:
        roles = {r[0] for r in conn.execute("SELECT role FROM user_roles r JOIN users u ON u.id=r.user_id WHERE u.username='owner'")}
    assert roles == {"Admin", "Builder"}
    cli.bootstrap_admin_from_env()                     # second start: no duplicate, just a reminder
    assert "remove the variable" in capsys.readouterr().out
    assert client_for("owner", password="a-long-enough-password").get("/api/auth/me").status_code == 200


def test_run_starts_at_project_floor(env, monkeypatch):
    """A question in a Public workspace starts Public (so it may reach a Public MCP server) on a public server;
    on a plant server a question never starts below Internal."""
    from nexagent.api import workflows as wf_api
    from nexagent.config import settings
    uid = make_user("floor1", ["Builder"])
    with db.tx() as conn:
        pid = db.new_id()
        conn.execute("INSERT INTO projects(id,name,description,classification_floor,created_by,created_at,require_review) VALUES(?,?,?,?,?,?,0)",
                     (pid, "p", "", "Public", uid, db.now()))
        version = {"id": "v-none", "valid": 1}
        plant = wf_api._start_run(conn, project_id=pid, version=version, question="q?", kind="test", user_id=None)
        monkeypatch.setattr(settings(), "public_mode", True)
        public = wf_api._start_run(conn, project_id=pid, version=version, question="q?", kind="test", user_id=None)
        cls = dict(conn.execute("SELECT id, classification FROM runs WHERE id IN (?,?)", (plant, public)).fetchall())
    assert cls == {plant: "Internal", public: "Public"}
