"""Model Improvement workspace: dataset approval, versions, training, evaluation, promotion, rollback."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db, jobs
from ..datasets import (approve_candidate, build_version, edit_candidate, load_candidate, proposals,
                        propose_splits)
from ..policy import Actor, Role, project_for, readable_kbs, require, require_admin
from ..security import CurrentActor
from ..training.coordinator import base_model_path_ok, request_cancel, rollback, validate_params
from ..training.runner import RECIPES

router = APIRouter()


class Strict(BaseModel):
    model_config = {"extra": "forbid"}


class CandidateDecision(Strict):
    decision: str = Field(pattern="^(include|exclude)$")
    note: str = Field(default="", max_length=1000)


class CandidateEdit(Strict):
    question: str = Field(min_length=1, max_length=4000)
    answer: str = Field(min_length=1, max_length=8000)


class VersionIn(Strict):
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=40)
    split_by_group: dict[str, str]


class BaseModelIn(Strict):
    name: str = Field(min_length=2, max_length=80)
    path: str = Field(min_length=2, max_length=500)
    license: str = Field(min_length=3, max_length=200)
    notes: str = Field(default="", max_length=1000)


class TrainingIn(Strict):
    dataset_version_id: str
    base_model_id: str
    recipe: str
    params: dict = {}
    seed: int = Field(default=42, ge=0, le=2**31 - 1)


class PromoteIn(Strict):
    evaluation_id: str
    alias: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]+$")
    note: str = Field(default="", max_length=1000)


def _train_role(actor: Actor):
    require(actor.has(Role.TRAINING_OPERATOR), "The Training Operator role is required")


def _can_see(conn, actor: Actor, cand: dict) -> bool:
    """Operators only see examples whose source documents sit in knowledge bases they can read."""
    docs = json.loads(cand["source_document_ids"]) if isinstance(cand["source_document_ids"], str) else cand["source_document_ids"]
    if not docs:
        return True
    kbs = {r[0] for r in conn.execute(f"SELECT kb_id FROM documents WHERE id IN ({','.join('?' * len(docs))})", docs)}
    return kbs <= readable_kbs(conn, actor.id, cand["project_id"])


# ------------------------------------------------------------------ candidates
@router.get("/projects/{pid}/dataset/candidates")
def candidates(pid: str, actor: Actor = CurrentActor):
    _train_role(actor)
    with db.read() as conn:
        project_for(conn, actor, pid)
        rows = db.all_rows(conn, """SELECT c.*, u.username AS reviewer_username FROM dataset_candidates c
            JOIN users u ON u.id=c.answer_reviewer WHERE c.project_id=? ORDER BY c.created_at DESC""", (pid,))
        cands = [load_candidate(r) for r in rows if _can_see(conn, actor, r)]
        for c in cands:
            c.pop("evidence_json", None)
            c["approvals"] = db.all_rows(conn, """SELECT a.decision, a.note, a.created_at, a.content_hash=? AS current,
                u.username FROM dataset_approvals a JOIN users u ON u.id=a.approver_id WHERE a.candidate_id=?
                ORDER BY a.created_at DESC""", (c["content_hash"], c["id"]))
    active = [c for c in cands if c["status"] != "excluded"]
    groups = sorted({c["group_id"] for c in cands if c["status"] == "approved"})
    return {"candidates": cands, "proposals": proposals(active), "proposal_method": "rule-based (no AI model)",
            "proposed_splits": propose_splits(groups) if groups else {}}


@router.post("/dataset/candidates/{cid}/decision")
def decide_candidate(cid: str, body: CandidateDecision, actor: Actor = CurrentActor):
    _train_role(actor)
    with db.tx() as conn:
        c = db.one(conn, "SELECT * FROM dataset_candidates WHERE id=?", (cid,))
        if not c or not _can_see(conn, actor, c):
            raise HTTPException(404, "Candidate not found")
        project_for(conn, actor, c["project_id"])
        try:
            approve_candidate(conn, cid, actor.id, body.decision, body.note)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    return {"recorded": True}


@router.put("/dataset/candidates/{cid}")
def edit(cid: str, body: CandidateEdit, actor: Actor = CurrentActor):
    _train_role(actor)
    with db.tx() as conn:
        c = db.one(conn, "SELECT * FROM dataset_candidates WHERE id=?", (cid,))
        if not c or not _can_see(conn, actor, c):
            raise HTTPException(404, "Candidate not found")
        project_for(conn, actor, c["project_id"])
        edit_candidate(conn, cid, body.question.strip(), body.answer.strip(), actor.id)
    return {"updated": True, "notice": "Earlier approvals no longer apply to the edited text."}


# ------------------------------------------------------------------ versions
@router.get("/projects/{pid}/dataset/versions")
def versions(pid: str, actor: Actor = CurrentActor):
    _train_role(actor)
    with db.read() as conn:
        project_for(conn, actor, pid)
        rows = db.all_rows(conn, """SELECT id,name,version,sha256,classification,counts_json,created_at,restricted_reason,
            (SELECT username FROM users WHERE id=created_by) AS created_by FROM dataset_versions WHERE project_id=?
            ORDER BY created_at DESC""", (pid,))
    for r in rows:
        r["counts"] = json.loads(r.pop("counts_json"))
    return rows


@router.post("/projects/{pid}/dataset/versions", status_code=201)
def create_version(pid: str, body: VersionIn, actor: Actor = CurrentActor):
    _train_role(actor)
    with db.tx() as conn:
        project_for(conn, actor, pid)
        try:
            return build_version(conn, pid, body.name.strip(), body.version.strip(), body.split_by_group, actor.id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None


@router.get("/dataset/versions/{vid}")
def version_detail(vid: str, actor: Actor = CurrentActor):
    _train_role(actor)
    with db.read() as conn:
        row = db.one(conn, "SELECT * FROM dataset_versions WHERE id=?", (vid,))
        if not row:
            raise HTTPException(404, "Not found")
        project_for(conn, actor, row["project_id"])
    m = json.loads(row["manifest_json"])
    return {"id": vid, "sha256": row["sha256"], "classification": row["classification"],
            "restricted_reason": row["restricted_reason"], "split_definition": m["split_definition"],
            "exclusions": m["exclusions"],
            "examples": [{k: e[k] for k in ("id", "group_id", "split", "question", "classification")} for e in m["examples"]]}


# ------------------------------------------------------------------ base models
@router.get("/training/base-models")
def base_models(actor: Actor = CurrentActor):
    require(actor.has(Role.TRAINING_OPERATOR, Role.ADMIN), "Training Operator or Admin role required")
    with db.read() as conn:
        return {"models": db.all_rows(conn, "SELECT * FROM base_models ORDER BY name"),
                "recipes": RECIPES}


@router.post("/training/base-models", status_code=201)
def register_base_model(body: BaseModelIn, actor: Actor = CurrentActor):
    require_admin(actor)
    try:
        path = base_model_path_ok(body.path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if not (path / "config.json").exists():
        raise HTTPException(400, f"{path} does not contain a Hugging Face model (config.json missing)")
    with db.tx() as conn:
        mid = db.new_id()
        conn.execute("INSERT INTO base_models VALUES(?,?,?,?,?,?,?)",
                     (mid, body.name, str(path), body.license, actor.id, body.notes, db.now()))
        db.audit(conn, actor.id, "training.base_model_registered", "base_model", mid,
                 {"name": body.name, "path": str(path), "license": body.license})
    return {"id": mid}


# ------------------------------------------------------------------ training jobs
@router.get("/projects/{pid}/training/jobs")
def list_jobs(pid: str, actor: Actor = CurrentActor):
    _train_role(actor)
    with db.read() as conn:
        project_for(conn, actor, pid)
        rows = db.all_rows(conn, """SELECT j.*, b.name AS base_model, d.name AS dataset_name, d.version AS dataset_version,
            (SELECT username FROM users WHERE id=j.requested_by) AS requested_by_name,
            (SELECT username FROM users WHERE id=j.approved_by) AS approved_by_name
            FROM training_jobs j JOIN base_models b ON b.id=j.base_model_id JOIN dataset_versions d ON d.id=j.dataset_version_id
            WHERE j.project_id=? ORDER BY j.created_at DESC""", (pid,))
    for r in rows:
        r["preflight"] = json.loads(r.pop("preflight_json") or "null")
    return rows


@router.post("/projects/{pid}/training/jobs", status_code=201)
def create_job(pid: str, body: TrainingIn, actor: Actor = CurrentActor):
    _train_role(actor)
    try:
        params = validate_params(body.recipe, body.params)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    with db.tx() as conn:
        project_for(conn, actor, pid)
        ds = db.one(conn, "SELECT * FROM dataset_versions WHERE id=? AND project_id=?", (body.dataset_version_id, pid))
        if not ds:
            raise HTTPException(404, "Dataset version not found in this project")
        if ds["restricted_reason"]:
            raise HTTPException(400, f"Dataset version is restricted: {ds['restricted_reason']}")
        if not conn.execute("SELECT 1 FROM base_models WHERE id=?", (body.base_model_id,)).fetchone():
            raise HTTPException(404, "Base model not registered")
        jid = db.new_id()
        conn.execute("""INSERT INTO training_jobs(id,project_id,dataset_version_id,base_model_id,recipe,params_json,seed,status,
            requested_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                     (jid, pid, body.dataset_version_id, body.base_model_id, body.recipe, json.dumps(params), body.seed,
                      "pending_approval", actor.id, db.now()))
        db.audit(conn, actor.id, "training.requested", "training_job", jid,
                 {"dataset_sha256": ds["sha256"], "recipe": body.recipe, "params": params, "seed": body.seed})
    return {"id": jid, "status": "pending_approval"}


@router.post("/training/jobs/{jid}/approve")
def approve_job(jid: str, actor: Actor = CurrentActor):
    require(actor.has(Role.TRAINING_OPERATOR, Role.ADMIN), "Training Operator or Admin role required")
    with db.tx() as conn:
        job = db.one(conn, "SELECT * FROM training_jobs WHERE id=?", (jid,))
        if not job:
            raise HTTPException(404, "Job not found")
        project_for(conn, actor, job["project_id"])
        if job["status"] != "pending_approval":
            raise HTTPException(409, f"Job is {job['status']}")
        if job["requested_by"] == actor.id:
            raise HTTPException(403, "A different person must approve a training job")
        conn.execute("UPDATE training_jobs SET approved_by=?, approved_at=? WHERE id=?", (actor.id, db.now(), jid))
        jobs.enqueue(conn, "training.preflight", {"job_id": jid}, idempotency_key="preflight:" + jid, max_attempts=1)
        db.audit(conn, actor.id, "training.approved", "training_job", jid, {})
    return {"approved": True, "next": "preflight checks are running"}


@router.post("/training/jobs/{jid}/reject")
def reject_job(jid: str, actor: Actor = CurrentActor):
    require(actor.has(Role.TRAINING_OPERATOR, Role.ADMIN), "Training Operator or Admin role required")
    with db.tx() as conn:
        job = db.one(conn, "SELECT * FROM training_jobs WHERE id=?", (jid,))
        project_for(conn, actor, job["project_id"])
        if job["status"] not in ("pending_approval", "blocked"):
            raise HTTPException(409, f"Job is {job['status']}")
        conn.execute("UPDATE training_jobs SET status='rejected', finished_at=? WHERE id=?", (db.now(), jid))
        db.audit(conn, actor.id, "training.rejected", "training_job", jid, {})
    return {"rejected": True}


@router.post("/training/jobs/{jid}/cancel")
def cancel_job(jid: str, actor: Actor = CurrentActor):
    _train_role(actor)
    with db.tx() as conn:
        job = db.one(conn, "SELECT * FROM training_jobs WHERE id=?", (jid,))
        project_for(conn, actor, job["project_id"])
        try:
            return {"status": request_cancel(conn, jid, actor.id)}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None


@router.get("/training/jobs/{jid}")
def job_detail(jid: str, actor: Actor = CurrentActor):
    _train_role(actor)
    with db.read() as conn:
        job = db.one(conn, "SELECT * FROM training_jobs WHERE id=?", (jid,))
        if not job:
            raise HTTPException(404, "Job not found")
        project_for(conn, actor, job["project_id"])
        metrics = db.all_rows(conn, "SELECT step,epoch,loss,eval_loss,learning_rate,created_at FROM training_metrics "
                                    "WHERE job_id=? ORDER BY step", (jid,))
        artifacts = db.all_rows(conn, "SELECT * FROM model_artifacts WHERE training_job_id=?", (jid,))
        evals = db.all_rows(conn, """SELECT e.* FROM evaluations e JOIN model_artifacts a ON a.id=e.artifact_id
            WHERE a.training_job_id=? ORDER BY e.created_at DESC""", (jid,))
        ds = db.one(conn, "SELECT sha256,name,version,classification FROM dataset_versions WHERE id=?",
                    (job["dataset_version_id"],))
        bm = db.one(conn, "SELECT name,path,license FROM base_models WHERE id=?", (job["base_model_id"],))
    for k in ("preflight_json", "software_json", "hardware_json", "params_json"):
        job[k.replace("_json", "")] = json.loads(job.pop(k) or "null")
    for a in artifacts:
        a["files"] = json.loads(a.pop("files_json"))
    for e in evals:
        e["results"] = json.loads(e.pop("results_json") or "null")
    return {"job": job, "metrics": metrics, "artifacts": artifacts, "evaluations": evals, "dataset": ds, "base_model": bm,
            "metrics_note": "Values are read from the training runner's own log; nothing is estimated or simulated."}


# ------------------------------------------------------------------ evaluation & promotion
@router.post("/artifacts/{aid}/evaluate", status_code=201)
def evaluate(aid: str, actor: Actor = CurrentActor):
    _train_role(actor)
    with db.tx() as conn:
        art = db.one(conn, """SELECT a.*, j.project_id, j.dataset_version_id FROM model_artifacts a
            JOIN training_jobs j ON j.id=a.training_job_id WHERE a.id=?""", (aid,))
        if not art:
            raise HTTPException(404, "Artifact not found")
        project_for(conn, actor, art["project_id"])
        if art["status"] != "registered":
            raise HTTPException(409, f"Artifact is {art['status']}")
        eid = db.new_id()
        conn.execute("INSERT INTO evaluations(id,artifact_id,dataset_version_id,status,requested_by,created_at) VALUES(?,?,?,?,?,?)",
                     (eid, aid, art["dataset_version_id"], "queued", actor.id, db.now()))
        jobs.enqueue(conn, "evaluation.run", {"evaluation_id": eid}, queue="training", max_attempts=1)
        db.audit(conn, actor.id, "evaluation.requested", "evaluation", eid, {"artifact": aid})
    return {"id": eid}


@router.get("/deployments")
def deployments(actor: Actor = CurrentActor):
    require(actor.has(Role.TRAINING_OPERATOR, Role.ADMIN) or "model.promote" in actor.permissions,
            "Training Operator, Admin or promotion permission required")
    with db.read() as conn:
        return {"deployments": db.all_rows(conn, """SELECT d.*, (SELECT username FROM users WHERE id=d.approved_by) AS approver
                    FROM deployments d ORDER BY d.created_at DESC"""),
                "aliases": db.all_rows(conn, "SELECT * FROM model_aliases")}


@router.post("/deployments", status_code=201)
def promote(body: PromoteIn, actor: Actor = CurrentActor):
    require("model.promote" in actor.permissions, "The model promotion permission is required")
    with db.tx() as conn:
        ev = db.one(conn, """SELECT e.*, a.status AS art_status, j.requested_by, j.project_id, a.id AS artifact_id
            FROM evaluations e JOIN model_artifacts a ON a.id=e.artifact_id JOIN training_jobs j ON j.id=a.training_job_id
            WHERE e.id=?""", (body.evaluation_id,))
        if not ev:
            raise HTTPException(404, "Evaluation not found")
        project_for(conn, actor, ev["project_id"])
        if ev["status"] != "completed":
            raise HTTPException(409, "Promotion needs a completed evaluation")
        if ev["art_status"] != "registered":
            raise HTTPException(409, f"Artifact is {ev['art_status']} and cannot be promoted")
        if ev["requested_by"] == actor.id:
            raise HTTPException(403, "The person who requested the training cannot approve its promotion")
        did = db.new_id()
        conn.execute("""INSERT INTO deployments(id,alias,artifact_id,evaluation_id,status,approved_by,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)""", (did, body.alias, ev["artifact_id"], ev["id"], "pending_export", actor.id,
                                         db.now(), db.now()))
        jobs.enqueue(conn, "deployment.export", {"deployment_id": did}, queue="training", max_attempts=1)
        db.audit(conn, actor.id, "deployment.approved", "deployment", did,
                 {"alias": body.alias, "evaluation": ev["id"], "note": body.note})
    return {"id": did, "status": "pending_export",
            "notice": "Nothing switches until the merged model is imported and answers through Ollama."}


@router.post("/deployments/{alias}/rollback")
def rollback_alias(alias: str, actor: Actor = CurrentActor):
    require("model.promote" in actor.permissions, "The model promotion permission is required")
    with db.tx() as conn:
        try:
            return rollback(conn, alias, actor.id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
