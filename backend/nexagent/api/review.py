"""Engineer review queue: evidence, restricted draft, findings, correction editor, decision history."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..datasets import create_candidate
from ..policy import Actor, Role, readable_kbs, require
from ..security import CurrentActor
from ..validators import CITE_RE, validate_answer

router = APIRouter()


class DecisionIn(BaseModel):
    model_config = {"extra": "forbid"}
    decision: str = Field(pattern="^(approve|correct|reject)$")
    answer: str = Field(default="", max_length=8000)
    note: str = Field(default="", max_length=2000)


def _visible(conn, actor: Actor, item: dict) -> bool:
    """Reviewers see an item only if they are project members with read access to every KB behind its evidence."""
    if not conn.execute("SELECT 1 FROM project_members WHERE project_id=? AND user_id=?",
                        (item["project_id"], actor.id)).fetchone():
        return False
    kbs = {e["kb_id"] for e in json.loads(item["evidence_json"])}
    return kbs <= readable_kbs(conn, actor.id, item["project_id"])


@router.get("/review/items")
def queue(status: str = "open", actor: Actor = CurrentActor):
    require(actor.has(Role.REVIEWER), "The Engineer Reviewer role is required")
    with db.read() as conn:
        rows = db.all_rows(conn, """SELECT i.*, p.name AS project_name, u.username AS assigned_username FROM review_items i
            JOIN projects p ON p.id=i.project_id LEFT JOIN users u ON u.id=i.assigned_to
            WHERE i.status=? ORDER BY i.created_at LIMIT 500""", (status,))
        out = []
        for r in rows:
            if _visible(conn, actor, r):
                f = json.loads(r["findings_json"])
                out.append({k: r[k] for k in ("id", "project_id", "project_name", "question", "classification",
                                              "status", "assigned_to", "assigned_username", "created_at")}
                           | {"issue_codes": sorted({i["code"] for i in f.get("issues", [])})})
        return out


def _item(conn, actor, iid):
    require(actor.has(Role.REVIEWER), "The Engineer Reviewer role is required")
    item = db.one(conn, "SELECT * FROM review_items WHERE id=?", (iid,))
    if not item or not _visible(conn, actor, item):
        raise HTTPException(404, "Review item not found or not permitted")
    return item


@router.get("/review/items/{iid}")
def get_item(iid: str, actor: Actor = CurrentActor):
    with db.read() as conn:
        item = _item(conn, actor, iid)
        decisions = db.all_rows(conn, """SELECT d.*, u.username, u.display_name FROM review_decisions d
            JOIN users u ON u.id=d.reviewer_id WHERE d.item_id=? ORDER BY d.created_at""", (iid,))
        fb = db.all_rows(conn, "SELECT rating, comment, created_at FROM feedback WHERE run_id=?", (item["run_id"],))
    item["evidence"] = json.loads(item.pop("evidence_json"))
    item["findings"] = json.loads(item.pop("findings_json"))
    for d in decisions:
        d["citations"] = json.loads(d.pop("citations_json"))
    return {"item": item, "decisions": decisions, "feedback": fb}


@router.post("/review/items/{iid}/assign")
def assign(iid: str, actor: Actor = CurrentActor):
    with db.tx() as conn:
        item = _item(conn, actor, iid)
        if item["status"] != "open":
            raise HTTPException(409, "Item is already closed")
        conn.execute("UPDATE review_items SET assigned_to=? WHERE id=?", (actor.id, iid))
        db.audit(conn, actor.id, "review.assigned", "review_item", iid, {})
    return {"assigned": True}


@router.post("/review/items/{iid}/check")
def check(iid: str, body: DecisionIn, actor: Actor = CurrentActor):
    """Run the deterministic validators on a proposed correction (advice for the engineer)."""
    with db.read() as conn:
        item = _item(conn, actor, iid)
    ev = json.loads(item["evidence_json"])
    return {"issues": [i.public() for i in validate_answer(item["question"], body.answer, ev)]}


@router.post("/review/items/{iid}/decide")
def decide(iid: str, body: DecisionIn, actor: Actor = CurrentActor):
    with db.tx() as conn:
        item = _item(conn, actor, iid)
        if item["status"] != "open":
            raise HTTPException(409, "This item has already been decided")
        evidence = json.loads(item["evidence_json"])
        answer = item["draft"] if body.decision == "approve" else body.answer.strip()
        refs: list[int] = []
        if body.decision in ("approve", "correct"):
            if not answer:
                raise HTTPException(400, "A corrected answer is required")
            refs = sorted({int(x) for m in CITE_RE.finditer(answer) for x in m.group(1).split(",")})
            valid_refs = {e["ref"] for e in evidence}
            if not refs or any(r not in valid_refs for r in refs):
                raise HTTPException(400, "The answer must cite the numbered sources shown (e.g. [1]); "
                                         "unknown source numbers are not allowed")
        did = db.new_id()
        conn.execute("""INSERT INTO review_decisions(id,item_id,reviewer_id,decision,answer,citations_json,note,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (did, iid, actor.id, body.decision, answer or None, json.dumps(refs), body.note,
                                         db.now()))
        status = {"approve": "approved", "correct": "corrected", "reject": "rejected"}[body.decision]
        conn.execute("UPDATE review_items SET status=?, closed_at=?, assigned_to=coalesce(assigned_to, ?) WHERE id=?",
                     (status, db.now(), actor.id, iid))
        cand = create_candidate(conn, did)        # a *proposal* only; inclusion needs a separate approval
        db.audit(conn, actor.id, "review." + status, "review_item", iid, {"decision": did, "candidate": cand})
    return {"decision_id": did, "status": status, "dataset_candidate": cand}
