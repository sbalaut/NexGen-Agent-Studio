"""Agent Builder: workflows, versions, runs (+SSE), assistants, API keys, feedback."""
from __future__ import annotations

import asyncio
import json
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import db, jobs
from ..graph import CATALOG, CONDITION_RULES, default_graph, graph_context, validate_graph
from ..documents import SECTION_TYPES
from ..policy import Actor, LEVEL, max_class, membership, project_for, readable_kbs, require
from ..security import CurrentActor, api_key_identity, key_verifier, session_actor
from ..config import settings

router = APIRouter()


class Strict(BaseModel):
    model_config = {"extra": "forbid"}


class WorkflowIn(Strict):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    kb_ids: list[str] = []
    model: str = Field(default="", max_length=120)
    review_model: str = Field(default="", max_length=120)
    template: str = Field(default="qa", pattern="^(qa|agent|chat)$")
    connection: str = Field(default="local-ollama", max_length=60)


class VersionIn(Strict):
    graph: dict
    base_version: int = Field(ge=0)


class AskIn(Strict):
    question: str = Field(min_length=1, max_length=4000)


class PublishIn(Strict):
    workflow_id: str
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=3, max_length=60, pattern=r"^[a-z0-9][a-z0-9-]+$")
    version_no: int | None = None


class KeyIn(Strict):
    name: str = Field(min_length=1, max_length=80)
    max_classification: str = Field(default="Internal", pattern="^(Public|Internal|Restricted)$")


class FeedbackIn(Strict):
    rating: int = Field(ge=-1, le=1)
    comment: str = Field(default="", max_length=2000)


# ------------------------------------------------------------------ catalog
@router.get("/builder/catalog")
def catalog(actor: Actor = CurrentActor):
    return {"nodes": CATALOG, "condition_rules": CONDITION_RULES, "section_types": SECTION_TYPES}


# ------------------------------------------------------------------ workflows
def _workflow(conn, actor, wid, need="read"):
    wf = db.one(conn, "SELECT * FROM workflows WHERE id=? AND archived=0", (wid,))
    if not wf:
        raise HTTPException(404, "Workflow not found")
    project_for(conn, actor, wf["project_id"], need)
    return wf


@router.get("/projects/{pid}/workflows")
def list_workflows(pid: str, actor: Actor = CurrentActor):
    with db.read() as conn:
        project_for(conn, actor, pid)
        return db.all_rows(conn, """SELECT w.*, v.valid FROM workflows w LEFT JOIN workflow_versions v
            ON v.workflow_id=w.id AND v.version_no=w.latest_version WHERE w.project_id=? AND w.archived=0
            ORDER BY w.created_at DESC""", (pid,))


def _save_version(conn, actor, wf, graph, base_version) -> dict:
    if base_version != wf["latest_version"]:
        raise HTTPException(409, f"This workflow was changed by someone else (now version {wf['latest_version']}). "
                                 "Reload to see their changes before saving.")
    allowed = readable_kbs(conn, actor.id, wf["project_id"])
    errors = validate_graph(graph, **graph_context(conn, actor.id, wf["project_id"]))
    no = wf["latest_version"] + 1
    vid = db.new_id()
    conn.execute("""INSERT INTO workflow_versions(id,workflow_id,version_no,graph_json,valid,validation_json,created_by,created_at)
        VALUES(?,?,?,?,?,?,?,?)""", (vid, wf["id"], no, json.dumps(graph), int(not errors), json.dumps(errors),
                                     actor.id, db.now()))
    conn.execute("UPDATE workflows SET latest_version=? WHERE id=?", (no, wf["id"]))
    db.audit(conn, actor.id, "workflow.version_saved", "workflow", wf["id"], {"version": no, "valid": not errors})
    return {"version_no": no, "version_id": vid, "valid": not errors, "errors": errors}


@router.post("/projects/{pid}/workflows", status_code=201)
def create_workflow(pid: str, body: WorkflowIn, actor: Actor = CurrentActor):
    with db.tx() as conn:
        p = project_for(conn, actor, pid, "write")
        wid = db.new_id()
        conn.execute("INSERT INTO workflows(id,project_id,name,description,created_by,created_at) VALUES(?,?,?,?,?,?)",
                     (wid, pid, body.name.strip(), body.description, actor.id, db.now()))
        wf = db.one(conn, "SELECT * FROM workflows WHERE id=?", (wid,))
        graph = default_graph(body.kb_ids, body.model, body.review_model, template=body.template,
                              connection=body.connection, require_review=bool(p["require_review"]))
        res = _save_version(conn, actor, wf, graph, 0)
    return {"id": wid, **res}


@router.get("/workflows/{wid}")
def get_workflow(wid: str, version: int | None = None, actor: Actor = CurrentActor):
    with db.read() as conn:
        wf = _workflow(conn, actor, wid)
        v = db.one(conn, "SELECT * FROM workflow_versions WHERE workflow_id=? AND version_no=?",
                   (wid, version or wf["latest_version"]))
        versions = db.all_rows(conn, """SELECT v.version_no, v.valid, v.created_at, u.username FROM workflow_versions v
            LEFT JOIN users u ON u.id=v.created_by WHERE workflow_id=? ORDER BY version_no DESC LIMIT 50""", (wid,))
        m = membership(conn, wf["project_id"], actor.id)
    return {"workflow": wf, "version": {**v, "graph": json.loads(v["graph_json"]), "errors": json.loads(v["validation_json"])},
            "versions": versions, "can_edit": actor.can_build and m in ("owner", "editor")}


@router.post("/workflows/{wid}/versions")
def save_version(wid: str, body: VersionIn, actor: Actor = CurrentActor):
    with db.tx() as conn:
        wf = _workflow(conn, actor, wid, "write")
        return _save_version(conn, actor, wf, body.graph, body.base_version)


@router.post("/workflows/{wid}/validate")
def validate_only(wid: str, body: VersionIn, actor: Actor = CurrentActor):
    with db.read() as conn:
        wf = _workflow(conn, actor, wid, "write")
        errors = validate_graph(body.graph, **graph_context(conn, actor.id, wf["project_id"]))
    return {"valid": not errors, "errors": errors}


@router.delete("/workflows/{wid}")
def archive_workflow(wid: str, actor: Actor = CurrentActor):
    with db.tx() as conn:
        wf = _workflow(conn, actor, wid, "manage")
        if conn.execute("SELECT 1 FROM assistants WHERE workflow_id=? AND status='published'", (wid,)).fetchone():
            raise HTTPException(409, "Unpublish the assistant that uses this workflow first")
        conn.execute("UPDATE workflows SET archived=1 WHERE id=?", (wid,))
        db.audit(conn, actor.id, "workflow.archived", "workflow", wid)
    return {"archived": True}


# ------------------------------------------------------------------ runs
def _start_run(conn, *, project_id, version, question, kind, user_id=None, assistant_id=None, api_key_id=None) -> str:
    if not version["valid"]:
        raise HTTPException(400, "This workflow version has validation errors. Fix them before running.")
    if settings().public_mode and user_id:
        n = conn.execute("SELECT count(*) FROM usage_events WHERE user_id=? AND kind='run' AND created_at>?",
                         (user_id, db.now() - 3600)).fetchone()[0]
        if n >= settings().quota_runs_per_hour:
            raise HTTPException(429, f"Usage limit reached ({settings().quota_runs_per_hour} questions per hour). "
                                     "Please try again later.")
        conn.execute("INSERT INTO usage_events(user_id,kind,amount,created_at) VALUES(?,?,?,?)",
                     (user_id, "run", 1, db.now()))
    rid = db.new_id()
    # The question itself starts at the project's minimum classification. Plant servers never treat a
    # question as lower than Internal (operators may type plant details into it).
    floor = (conn.execute("SELECT classification_floor FROM projects WHERE id=?", (project_id,)).fetchone() or ["Internal"])[0]
    start_cls = floor if settings().public_mode else max_class(floor, "Internal")
    conn.execute("""INSERT INTO runs(id,project_id,workflow_version_id,assistant_id,user_id,api_key_id,kind,question,status,
        created_at,classification) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (rid, project_id, version["id"], assistant_id, user_id,
                                                                     api_key_id, kind, question.strip(), "queued", db.now(), start_cls))
    jobs.enqueue(conn, "run.execute", {"run_id": rid}, idempotency_key="run:" + rid, max_attempts=1)
    return rid


@router.post("/workflows/{wid}/test")
def test_run(wid: str, body: AskIn, version: int | None = None, actor: Actor = CurrentActor):
    with db.tx() as conn:
        wf = _workflow(conn, actor, wid, "write")
        v = db.one(conn, "SELECT * FROM workflow_versions WHERE workflow_id=? AND version_no=?",
                   (wid, version or wf["latest_version"]))
        rid = _start_run(conn, project_id=wf["project_id"], version=v, question=body.question, kind="test",
                         user_id=actor.id)
    return {"run_id": rid}


def _run_for(conn, actor: Actor, rid: str) -> dict:
    run = db.one(conn, "SELECT * FROM runs WHERE id=?", (rid,))
    if not run:
        raise HTTPException(404, "Run not found")
    m = membership(conn, run["project_id"], actor.id)
    own = run["user_id"] == actor.id and m is not None
    builder = actor.can_build and m in ("owner", "editor")
    if not (own or builder):
        raise HTTPException(404, "Run not found")
    run["_builder"] = builder
    return run


def _public_run(conn, run: dict) -> dict:
    out = {k: run[k] for k in ("id", "kind", "question", "status", "created_at", "finished_at", "error", "classification")}
    out["answer"] = json.loads(run["answer_json"]) if run["answer_json"] else None
    item = db.one(conn, "SELECT id,status FROM review_items WHERE run_id=?", (run["id"],))
    if item and item["status"] in ("approved", "corrected"):
        d = db.one(conn, "SELECT answer, citations_json, created_at FROM review_decisions WHERE item_id=? "
                         "ORDER BY created_at DESC LIMIT 1", (item["id"],))
        ev = {e["ref"]: e for e in json.loads(db.one(conn, "SELECT evidence_json FROM review_items WHERE id=?",
                                                         (item["id"],))["evidence_json"])}
        # release re-check: cited documents must still exist and still be readable by this user
        refs = json.loads(d["citations_json"])
        docs = {ev[r]["document_id"] for r in refs if r in ev}
        if run["user_id"]:
            kbs = readable_kbs(conn, run["user_id"], run["project_id"])
        elif run["api_key_id"] and conn.execute("SELECT 1 FROM api_keys WHERE id=? AND revoked_at IS NULL",
                                                (run["api_key_id"],)).fetchone():
            kbs = {r[0] for r in conn.execute("SELECT kb_id FROM api_key_kbs WHERE key_id=?", (run["api_key_id"],))}
        else:
            kbs = set()
        live = {r[0] for r in conn.execute(f"SELECT id FROM documents WHERE id IN ({','.join('?' * len(docs))}) "
                                           "AND deleted_at IS NULL AND kb_id IN (SELECT value FROM json_each(?))",
                                           [*docs, json.dumps(sorted(kbs))])} if docs else set()
        if docs and live == docs:
            out["answer"] = {"answer": d["answer"], "review": {"verdict": "engineer_" + item["status"]},
                             "citations": [{k: ev[r][k] for k in ("ref", "filename", "revision_no", "section", "location", "text")}
                                           for r in refs if r in ev],
                             "notice": "Checked and approved by an engineer."}
        else:
            out["answer"] = {"answer": "", "citations": [], "review": {"verdict": "withheld"},
                             "notice": "The engineer-approved answer is withheld because a cited source is no longer accessible."}
    elif item and item["status"] == "rejected":
        out["answer"] = {"answer": "", "citations": [], "review": {"verdict": "engineer_rejected"},
                         "notice": "An engineer reviewed this question and could not approve an answer from the sources."}
    return out


@router.get("/runs/{rid}")
def get_run(rid: str, actor: Actor = CurrentActor):
    with db.read() as conn:
        run = _run_for(conn, actor, rid)
        out = _public_run(conn, run)
        if run["_builder"]:
            out["nodes"] = db.all_rows(conn, "SELECT * FROM run_nodes WHERE run_id=? ORDER BY started_at", (rid,))
            for n in out["nodes"]:
                n["detail"] = json.loads(n["detail"]) if n["detail"] else None
            out["pinned_models"] = json.loads(run["pinned_json"] or "{}")
        else:
            out["nodes"] = db.all_rows(conn, "SELECT node_id,node_type,status,duration_ms,summary,error FROM run_nodes "
                                             "WHERE run_id=? ORDER BY started_at", (rid,))
    return out


@router.post("/runs/{rid}/cancel")
def cancel_run(rid: str, actor: Actor = CurrentActor):
    with db.tx() as conn:
        run = _run_for(conn, actor, rid)
        if run["status"] not in ("queued", "running"):
            raise HTTPException(409, f"Run is already {run['status']}")
        conn.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (rid,))
        if run["status"] == "queued":
            conn.execute("UPDATE runs SET status='cancelled', finished_at=?, error='Cancelled before start' WHERE id=?",
                         (db.now(), rid))
    return {"cancel_requested": True}


@router.get("/runs/{rid}/events")
async def run_events(rid: str, request: Request, after: int = 0):
    """Authenticated SSE with durable sequence numbers (resume with ?after=N or Last-Event-ID)."""
    actor = session_actor(request.cookies.get(settings().cookie_name, ""))
    if actor is None:
        raise HTTPException(401, "Sign in to continue")
    with db.read() as conn:
        _run_for(conn, actor, rid)
    last = int(request.headers.get("last-event-id") or after)

    async def stream():
        nonlocal last
        idle = 0
        while idle < 1800:
            if await request.is_disconnected():
                return
            with db.read() as conn:
                events = db.all_rows(conn, "SELECT seq,type,data FROM run_events WHERE run_id=? AND seq>? ORDER BY seq",
                                     (rid, last))
            for e in events:
                last = e["seq"]
                yield f"id: {e['seq']}\nevent: {e['type']}\ndata: {e['data']}\n\n"
                if e["type"] == "final":
                    return
            idle = 0 if events else idle + 1
            if not events:
                yield ": keep-alive\n\n" if idle % 30 == 0 else ""
            await asyncio.sleep(0.5)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/me/history")
def history(actor: Actor = CurrentActor):
    with db.read() as conn:
        runs = db.all_rows(conn, """SELECT r.* FROM runs r WHERE r.user_id=? AND r.kind='chat'
            AND EXISTS(SELECT 1 FROM project_members m WHERE m.project_id=r.project_id AND m.user_id=r.user_id)
            ORDER BY r.created_at DESC LIMIT 100""", (actor.id,))
        return [_public_run(conn, r) | {"assistant_id": r["assistant_id"]} for r in runs]


@router.post("/runs/{rid}/feedback")
def feedback(rid: str, body: FeedbackIn, actor: Actor = CurrentActor):
    """A rating is only a rating: it never makes an answer eligible for training."""
    with db.tx() as conn:
        _run_for(conn, actor, rid)
        conn.execute("""INSERT INTO feedback(id,run_id,user_id,rating,comment,created_at) VALUES(?,?,?,?,?,?)
            ON CONFLICT(run_id,user_id) DO UPDATE SET rating=excluded.rating, comment=excluded.comment""",
                     (db.new_id(), rid, actor.id, body.rating, body.comment, db.now()))
    return {"recorded": True}


# ------------------------------------------------------------------ assistants
@router.get("/projects/{pid}/assistants")
def list_assistants(pid: str, actor: Actor = CurrentActor):
    with db.read() as conn:
        project_for(conn, actor, pid)
        rows = db.all_rows(conn, """SELECT a.*, v.version_no, w.name AS workflow_name FROM assistants a
            LEFT JOIN workflow_versions v ON v.id=a.version_id JOIN workflows w ON w.id=a.workflow_id
            WHERE a.project_id=? ORDER BY a.created_at DESC""", (pid,))
        for r in rows:
            r["keys"] = db.all_rows(conn, "SELECT id,name,prefix,max_classification,created_at,revoked_at FROM api_keys "
                                          "WHERE assistant_id=? ORDER BY created_at DESC", (r["id"],))
    return rows


@router.get("/assistants")
def my_assistants(actor: Actor = CurrentActor):
    with db.read() as conn:
        return db.all_rows(conn, """SELECT a.id,a.name,a.slug,a.project_id,p.name AS project_name FROM assistants a
            JOIN projects p ON p.id=a.project_id JOIN project_members m ON m.project_id=a.project_id AND m.user_id=?
            WHERE a.status='published' ORDER BY a.name""", (actor.id,))


@router.post("/projects/{pid}/assistants")
def publish(pid: str, body: PublishIn, actor: Actor = CurrentActor):
    with db.tx() as conn:
        project_for(conn, actor, pid, "manage")
        wf = db.one(conn, "SELECT * FROM workflows WHERE id=? AND project_id=? AND archived=0", (body.workflow_id, pid))
        if not wf:
            raise HTTPException(404, "Workflow not found in this project")
        v = db.one(conn, "SELECT * FROM workflow_versions WHERE workflow_id=? AND version_no=?",
                   (wf["id"], body.version_no or wf["latest_version"]))
        if not v or not v["valid"]:
            raise HTTPException(400, "Only a valid workflow version can be published")
        existing = db.one(conn, "SELECT * FROM assistants WHERE slug=?", (body.slug,))
        if existing and existing["project_id"] != pid:
            raise HTTPException(409, "That address is already used by another assistant")
        if existing:
            conn.execute("""UPDATE assistants SET name=?, workflow_id=?, previous_version_id=version_id, version_id=?,
                status='published', updated_at=? WHERE id=?""", (body.name, wf["id"], v["id"], db.now(), existing["id"]))
            aid = existing["id"]
        else:
            aid = db.new_id()
            conn.execute("""INSERT INTO assistants(id,project_id,workflow_id,slug,name,version_id,status,created_by,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (aid, pid, wf["id"], body.slug, body.name, v["id"], "published", actor.id,
                                                 db.now(), db.now()))
        db.audit(conn, actor.id, "assistant.published", "assistant", aid, {"workflow": wf["id"], "version": v["version_no"]})
    return {"id": aid, "slug": body.slug, "version_no": v["version_no"]}


def _assistant(conn, actor, aid, need="manage"):
    a = db.one(conn, "SELECT * FROM assistants WHERE id=?", (aid,))
    if not a:
        raise HTTPException(404, "Assistant not found")
    project_for(conn, actor, a["project_id"], need)
    return a


@router.post("/assistants/{aid}/rollback")
def rollback_assistant(aid: str, actor: Actor = CurrentActor):
    with db.tx() as conn:
        a = _assistant(conn, actor, aid)
        if not a["previous_version_id"]:
            raise HTTPException(409, "There is no previous published version")
        conn.execute("UPDATE assistants SET version_id=previous_version_id, previous_version_id=version_id, updated_at=? "
                     "WHERE id=?", (db.now(), aid))
        db.audit(conn, actor.id, "assistant.rolled_back", "assistant", aid, {})
    return {"rolled_back": True}


@router.post("/assistants/{aid}/unpublish")
def unpublish(aid: str, actor: Actor = CurrentActor):
    with db.tx() as conn:
        _assistant(conn, actor, aid)
        conn.execute("UPDATE assistants SET status='unpublished', updated_at=? WHERE id=?", (db.now(), aid))
        conn.execute("UPDATE api_keys SET revoked_at=? WHERE assistant_id=? AND revoked_at IS NULL", (db.now(), aid))
        db.audit(conn, actor.id, "assistant.unpublished", "assistant", aid, {})
    return {"unpublished": True}


@router.post("/assistants/{aid}/ask")
def ask(aid: str, body: AskIn, actor: Actor = CurrentActor):
    with db.tx() as conn:
        a = db.one(conn, "SELECT * FROM assistants WHERE id=? AND status='published'", (aid,))
        if not a or membership(conn, a["project_id"], actor.id) is None:
            raise HTTPException(404, "Assistant not found or not shared with you")
        v = db.one(conn, "SELECT * FROM workflow_versions WHERE id=?", (a["version_id"],))
        rid = _start_run(conn, project_id=a["project_id"], version=v, question=body.question, kind="chat",
                         user_id=actor.id, assistant_id=aid)
    return {"run_id": rid}


@router.post("/assistants/{aid}/keys", status_code=201)
def create_key(aid: str, body: KeyIn, actor: Actor = CurrentActor):
    with db.tx() as conn:
        a = _assistant(conn, actor, aid)
        v = db.one(conn, "SELECT graph_json FROM workflow_versions WHERE id=?", (a["version_id"],))
        graph = json.loads(v["graph_json"])
        wanted = {k for n in graph["nodes"] if n["type"] == "retrieval" for k in (n.get("data") or {}).get("kb_ids", [])}
        mine = readable_kbs(conn, actor.id, a["project_id"])
        kbs = wanted & mine
        kid, prefix, secret = db.new_id(), secrets.token_hex(6), secrets.token_urlsafe(32)
        conn.execute("""INSERT INTO api_keys(id,assistant_id,name,prefix,verifier,max_classification,created_by,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (kid, aid, body.name, prefix, key_verifier(secret), body.max_classification,
                                         actor.id, db.now()))
        conn.executemany("INSERT INTO api_key_kbs VALUES(?,?)", [(kid, k) for k in sorted(kbs)])
        db.audit(conn, actor.id, "api_key.created", "assistant", aid, {"key": prefix, "kbs": sorted(kbs),
                                                                       "max_classification": body.max_classification})
    return {"id": kid, "key": f"nxk_{prefix}_{secret}", "knowledge_bases": sorted(kbs),
            "notice": "Copy this key now. Only a verifier is stored; it cannot be shown again."}


@router.post("/keys/{kid}/revoke")
def revoke_key(kid: str, actor: Actor = CurrentActor):
    with db.tx() as conn:
        k = db.one(conn, "SELECT * FROM api_keys WHERE id=?", (kid,))
        if not k:
            raise HTTPException(404, "Key not found")
        _assistant(conn, actor, k["assistant_id"])
        conn.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (db.now(), kid))
        conn.execute("UPDATE runs SET cancel_requested=1 WHERE api_key_id=? AND status IN ('queued','running')", (kid,))
        db.audit(conn, actor.id, "api_key.revoked", "api_key", kid, {})
    return {"revoked": True}


# ------------------------------------------------------------------ service API (API keys)
@router.post("/v1/assistants/{slug}/ask")
def api_ask(slug: str, body: AskIn, key: dict = Depends(api_key_identity)):
    if key["slug"] != slug:
        raise HTTPException(403, "This key belongs to a different assistant")
    with db.tx() as conn:
        a = db.one(conn, "SELECT * FROM assistants WHERE slug=? AND status='published'", (slug,))
        if not a:
            raise HTTPException(404, "Assistant not published")
        v = db.one(conn, "SELECT * FROM workflow_versions WHERE id=?", (a["version_id"],))
        rid = _start_run(conn, project_id=a["project_id"], version=v, question=body.question, kind="api",
                         assistant_id=a["id"], api_key_id=key["id"])
    return {"run_id": rid, "status_url": f"api/v1/runs/{rid}"}


@router.get("/v1/runs/{rid}")
def api_run(rid: str, key: dict = Depends(api_key_identity)):
    with db.read() as conn:
        run = db.one(conn, "SELECT * FROM runs WHERE id=? AND api_key_id=?", (rid, key["id"]))
        if not run:
            raise HTTPException(404, "Run not found")
        out = _public_run(conn, run)
    if out["answer"] and LEVEL[run["classification"]] > LEVEL[key["max_classification"]]:
        out["answer"] = {"answer": "", "citations": [], "notice": "Result exceeds this key's classification limit."}
    return out
