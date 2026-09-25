"""Projects, membership, knowledge bases, grants, documents and indexing."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .. import db, jobs
from ..config import settings
from ..datasets import flag_document_deletion
from ..documents import SECTION_TYPES
from ..llm import usable_connection_names
from ..policy import LEVEL, Actor, kb_for, project_for, require
from ..security import CurrentActor

router = APIRouter()
ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md", ".markdown"}


class Strict(BaseModel):
    model_config = {"extra": "forbid"}


class ProjectIn(Strict):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    classification_floor: str = Field(default="Internal", pattern="^(Public|Internal|Restricted)$")
    require_review: bool | None = None


class MemberIn(Strict):
    username: str = Field(min_length=3, max_length=64)
    membership: str = Field(pattern="^(owner|editor|viewer)$")


class KBIn(Strict):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    classification: str = Field(default="Internal", pattern="^(Public|Internal|Restricted)$")
    embedding_connection: str | None = Field(default=None, max_length=60)
    embedding_model: str | None = Field(default=None, max_length=120)
    graph_mode: str = Field(default="off", pattern="^(off|rules|llm)$")
    graph_connection: str | None = Field(default=None, max_length=60)
    graph_model: str | None = Field(default=None, max_length=120)


class KBSettingsIn(Strict):
    embedding_connection: str | None = Field(default=None, max_length=60)
    embedding_model: str | None = Field(default=None, max_length=120)
    graph_mode: str = Field(default="off", pattern="^(off|rules|llm)$")
    graph_connection: str | None = Field(default=None, max_length=60)
    graph_model: str | None = Field(default=None, max_length=120)


class GrantIn(Strict):
    user_id: str
    can_read: bool = True
    can_write: bool = False


class BlockEdit(Strict):
    id: str
    section_type: str | None = None
    include: bool | None = None


class BlocksIn(Strict):
    blocks: list[BlockEdit] = Field(max_length=5000)
    heading_types: dict[str, str] = {}


# ------------------------------------------------------------------ projects
@router.get("/projects")
def list_projects(actor: Actor = CurrentActor):
    with db.read() as conn:
        return db.all_rows(conn, """SELECT p.*, m.membership,
            (SELECT count(*) FROM knowledge_bases b JOIN kb_grants g ON g.kb_id=b.id AND g.user_id=m.user_id
             AND g.can_read=1 WHERE b.project_id=p.id) AS knowledge_bases,
            (SELECT count(*) FROM workflows w WHERE w.project_id=p.id AND w.archived=0) AS workflows,
            (SELECT count(*) FROM assistants a WHERE a.project_id=p.id AND a.status='published') AS assistants
            FROM projects p JOIN project_members m ON m.project_id=p.id AND m.user_id=? ORDER BY p.created_at DESC""",
                           (actor.id,))


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn, actor: Actor = CurrentActor):
    require(actor.can_build, "A Builder or Admin account is required")
    pid = db.new_id()
    with db.tx() as conn:
        require_review = body.require_review
        if require_review is None:
            require_review = not settings().public_mode
        if not require_review and not settings().public_mode and not actor.is_admin:
            raise HTTPException(403, "Only an administrator may create a project without mandatory answer review")
        conn.execute("""INSERT INTO projects(id,name,description,classification_floor,created_by,created_at,require_review)
            VALUES(?,?,?,?,?,?,?)""", (pid, body.name.strip(), body.description, body.classification_floor, actor.id,
                                       db.now(), int(require_review)))
        conn.execute("INSERT INTO project_members VALUES(?,?,?)", (pid, actor.id, "owner"))
        db.audit(conn, actor.id, "project.create", "project", pid, {"classification_floor": body.classification_floor})
    return {"id": pid}


@router.get("/projects/{pid}")
def get_project(pid: str, actor: Actor = CurrentActor):
    with db.read() as conn:
        return project_for(conn, actor, pid)


@router.get("/projects/{pid}/members")
def members(pid: str, actor: Actor = CurrentActor):
    with db.read() as conn:
        project_for(conn, actor, pid)
        rows = db.all_rows(conn, """SELECT m.user_id, m.membership, u.username, u.display_name, u.active FROM project_members m
            JOIN users u ON u.id=m.user_id WHERE m.project_id=? ORDER BY u.username""", (pid,))
        for r in rows:
            r["roles"] = sorted(x[0] for x in conn.execute("SELECT role FROM user_roles WHERE user_id=?", (r["user_id"],)))
        return rows


@router.put("/projects/{pid}/members")
def set_member(pid: str, body: MemberIn, actor: Actor = CurrentActor):
    with db.tx() as conn:
        project_for(conn, actor, pid, "manage")
        target = db.one(conn, "SELECT id FROM users WHERE username=? AND active=1", (body.username.strip().lower(),))
        if not target:
            raise HTTPException(404, "Active account not found. Ask an administrator to create it.")
        roles = {r[0] for r in conn.execute("SELECT role FROM user_roles WHERE user_id=?", (target["id"],))}
        if body.membership != "viewer" and not roles & {"Admin", "Builder"}:
            raise HTTPException(400, "Only Builder/Admin accounts can be project owners or editors")
        old = conn.execute("SELECT membership FROM project_members WHERE project_id=? AND user_id=?",
                           (pid, target["id"])).fetchone()
        if old and old[0] == "owner" and body.membership != "owner":
            if conn.execute("SELECT count(*) FROM project_members WHERE project_id=? AND membership='owner'",
                            (pid,)).fetchone()[0] <= 1:
                raise HTTPException(409, "Keep at least one project owner")
        conn.execute("""INSERT INTO project_members VALUES(?,?,?) ON CONFLICT(project_id,user_id)
            DO UPDATE SET membership=excluded.membership""", (pid, target["id"], body.membership))
        if body.membership == "owner":     # a new owner can manage every KB of the project (limitation #6 fixed)
            conn.execute("""INSERT INTO kb_grants(kb_id,user_id,can_read,can_write)
                SELECT id, ?, 1, 1 FROM knowledge_bases WHERE project_id=?
                ON CONFLICT(kb_id,user_id) DO UPDATE SET can_read=1, can_write=1""", (target["id"], pid))
        db.audit(conn, actor.id, "project.member_set", "project", pid,
                 {"user_id": target["id"], "membership": body.membership})
    return {"updated": True}


@router.delete("/projects/{pid}/members/{uid}")
def remove_member(pid: str, uid: str, actor: Actor = CurrentActor):
    with db.tx() as conn:
        if uid != actor.id:
            project_for(conn, actor, pid, "manage")
        old = conn.execute("SELECT membership FROM project_members WHERE project_id=? AND user_id=?", (pid, uid)).fetchone()
        if not old:
            raise HTTPException(404, "Member not found")
        if old[0] == "owner" and conn.execute(
                "SELECT count(*) FROM project_members WHERE project_id=? AND membership='owner'", (pid,)).fetchone()[0] <= 1:
            raise HTTPException(409, "Keep at least one project owner")
        conn.execute("DELETE FROM project_members WHERE project_id=? AND user_id=?", (pid, uid))  # trigger drops KB grants
        db.audit(conn, actor.id, "project.member_remove", "project", pid, {"user_id": uid})
    return {"removed": True}


# ------------------------------------------------------------------ knowledge bases
@router.get("/projects/{pid}/knowledge-bases")
def list_kbs(pid: str, actor: Actor = CurrentActor):
    with db.read() as conn:
        project_for(conn, actor, pid)
        rows = db.all_rows(conn, """SELECT b.*, g.can_write,
            (SELECT count(*) FROM documents d WHERE d.kb_id=b.id AND d.deleted_at IS NULL) AS documents,
            (SELECT chunk_count FROM index_generations WHERE id=b.active_generation) AS chunks,
            (SELECT status FROM index_generations WHERE kb_id=b.id ORDER BY created_at DESC LIMIT 1) AS last_index_status,
            (SELECT error FROM index_generations WHERE kb_id=b.id ORDER BY created_at DESC LIMIT 1) AS last_index_error
            FROM knowledge_bases b JOIN kb_grants g ON g.kb_id=b.id AND g.user_id=? AND g.can_read=1
            WHERE b.project_id=? ORDER BY b.created_at DESC""", (actor.id, pid))
        return rows


@router.post("/projects/{pid}/knowledge-bases", status_code=201)
def create_kb(pid: str, body: KBIn, actor: Actor = CurrentActor):
    kid = db.new_id()
    with db.tx() as conn:
        p = project_for(conn, actor, pid, "write")
        if LEVEL[body.classification] < LEVEL[p["classification_floor"]]:
            raise HTTPException(400, "Knowledge-base classification cannot be below the project floor")
        if conn.execute("SELECT 1 FROM knowledge_bases WHERE project_id=? AND name=?", (pid, body.name.strip())).fetchone():
            raise HTTPException(409, "A knowledge base with this name already exists")
        names = usable_connection_names(conn, actor.id)
        for label, value in (("Embedding", body.embedding_connection), ("Graph", body.graph_connection)):
            if value and value not in names:
                raise HTTPException(400, f"{label} connection '{value}' is not available to you")
        if body.graph_mode != "off" and not settings().has("graphrag"):
            raise HTTPException(400, "GraphRAG is not enabled on this server")
        conn.execute("""INSERT INTO knowledge_bases(id,project_id,name,description,classification,created_at,created_by,
            embedding_connection,embedding_model,graph_mode,graph_connection,graph_model) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (kid, pid, body.name.strip(), body.description, body.classification, db.now(), actor.id,
                      body.embedding_connection, body.embedding_model, body.graph_mode, body.graph_connection,
                      body.graph_model))
        conn.execute("""INSERT INTO kb_grants(kb_id,user_id,can_read,can_write)
            SELECT ?, user_id, 1, 1 FROM project_members WHERE project_id=? AND (membership='owner' OR user_id=?)""",
                     (kid, pid, actor.id))
        db.audit(conn, actor.id, "kb.create", "knowledge_base", kid, {"classification": body.classification})
    return {"id": kid}


@router.put("/knowledge-bases/{kid}/settings")
def kb_settings(kid: str, body: KBSettingsIn, actor: Actor = CurrentActor):
    """Models used to index this knowledge base. Changes apply at the next 'Build search index'."""
    with db.tx() as conn:
        kb = kb_for(conn, actor, kid, "write")
        owner = kb.get("created_by") or actor.id
        names = usable_connection_names(conn, owner)
        for label, value in (("Embedding", body.embedding_connection), ("Graph", body.graph_connection)):
            if value and value not in names:
                raise HTTPException(400, f"{label} connection '{value}' is not available to the knowledge-base owner")
        if body.graph_mode != "off" and not settings().has("graphrag"):
            raise HTTPException(400, "GraphRAG is not enabled on this server")
        conn.execute("""UPDATE knowledge_bases SET embedding_connection=?, embedding_model=?, graph_mode=?, graph_connection=?,
            graph_model=?, created_by=coalesce(created_by, ?) WHERE id=?""",
                     (body.embedding_connection, body.embedding_model, body.graph_mode, body.graph_connection,
                      body.graph_model, actor.id, kid))
        db.audit(conn, actor.id, "kb.settings_changed", "knowledge_base", kid, body.model_dump())
    return {"updated": True, "notice": "Rebuild the search index to apply the new settings."}


@router.get("/knowledge-bases/{kid}/graph")
def kb_graph(kid: str, q: str = "", limit: int = 60, actor: Actor = CurrentActor):
    """Entities (most connected, or matching q) and the relations among them, from the active index."""
    limit = max(5, min(limit, 200))
    with db.read() as conn:
        kb = kb_for(conn, actor, kid)
        gen = kb["active_generation"]
        if not gen:
            return {"entities": [], "relations": [], "communities": [], "status": "not indexed"}
        status = conn.execute("SELECT graph_status FROM index_generations WHERE id=?", (gen,)).fetchone()[0]
        if q.strip():
            like = "%" + q.strip().lower() + "%"
            ents = db.all_rows(conn, """SELECT id,name,type,description,community,degree FROM graph_entities
                WHERE generation_id=? AND norm LIKE ? ORDER BY degree DESC LIMIT ?""", (gen, like, limit))
            if ents:   # add direct neighbours of the matches
                ids = [e["id"] for e in ents]
                marks = ",".join("?" * len(ids))
                more = db.all_rows(conn, f"""SELECT DISTINCT e.id,e.name,e.type,e.description,e.community,e.degree
                    FROM graph_relations r JOIN graph_entities e ON e.id = CASE WHEN r.src IN ({marks}) THEN r.dst ELSE r.src END
                    WHERE r.generation_id=? AND (r.src IN ({marks}) OR r.dst IN ({marks})) ORDER BY e.degree DESC LIMIT ?""",
                                   ids + [gen] + ids + ids + [limit])
                seen = {e["id"] for e in ents}
                ents += [e for e in more if e["id"] not in seen]
        else:
            ents = db.all_rows(conn, """SELECT id,name,type,description,community,degree FROM graph_entities
                WHERE generation_id=? ORDER BY degree DESC LIMIT ?""", (gen, limit))
        ids = [e["id"] for e in ents]
        rels = []
        if ids:
            marks = ",".join("?" * len(ids))
            rels = db.all_rows(conn, f"""SELECT src, dst, relation, sum(weight) AS weight FROM graph_relations
                WHERE generation_id=? AND src IN ({marks}) AND dst IN ({marks}) GROUP BY src, dst, relation
                ORDER BY weight DESC LIMIT 400""", [gen] + ids + ids)
        comms = db.all_rows(conn, """SELECT community, title, summary, entity_count FROM graph_communities
            WHERE generation_id=? ORDER BY entity_count DESC LIMIT 20""", (gen,))
    return {"entities": ents, "relations": rels, "communities": comms, "status": status or "no graph (GraphRAG off)"}


@router.get("/knowledge-bases/{kid}/grants")
def kb_grants(kid: str, actor: Actor = CurrentActor):
    with db.read() as conn:
        kb = kb_for(conn, actor, kid, "manage")
        return db.all_rows(conn, """SELECT m.user_id, u.username, u.display_name, m.membership,
            coalesce(g.can_read,0) can_read, coalesce(g.can_write,0) can_write FROM project_members m
            JOIN users u ON u.id=m.user_id LEFT JOIN kb_grants g ON g.kb_id=? AND g.user_id=m.user_id
            WHERE m.project_id=? ORDER BY u.username""", (kid, kb["project_id"]))


@router.put("/knowledge-bases/{kid}/grants")
def set_grant(kid: str, body: GrantIn, actor: Actor = CurrentActor):
    with db.tx() as conn:
        kb = kb_for(conn, actor, kid, "manage")
        target = db.one(conn, """SELECT m.membership FROM project_members m JOIN users u ON u.id=m.user_id
            WHERE m.project_id=? AND m.user_id=? AND u.active=1""", (kb["project_id"], body.user_id))
        if not target:
            raise HTTPException(400, "Access can only be granted to active project members")
        if body.can_write and (not body.can_read or target["membership"] == "viewer"):
            raise HTTPException(400, "Write access needs read access and an owner/editor membership")
        if body.user_id == actor.id and not body.can_read:
            raise HTTPException(400, "Ask another project owner to remove your own access")
        if not body.can_read:
            conn.execute("DELETE FROM kb_grants WHERE kb_id=? AND user_id=?", (kid, body.user_id))
        else:
            conn.execute("""INSERT INTO kb_grants VALUES(?,?,?,?) ON CONFLICT(kb_id,user_id)
                DO UPDATE SET can_read=excluded.can_read, can_write=excluded.can_write""",
                         (kid, body.user_id, 1, int(body.can_write)))
        db.audit(conn, actor.id, "kb.grant_set", "knowledge_base", kid, body.model_dump())
    return {"updated": True}


# ------------------------------------------------------------------ documents
@router.get("/knowledge-bases/{kid}/documents")
def list_documents(kid: str, actor: Actor = CurrentActor):
    with db.read() as conn:
        kb_for(conn, actor, kid)
        docs = db.all_rows(conn, """SELECT d.id, d.filename, d.classification, d.created_at, d.family_id,
            r.id AS revision_id, r.revision_no, r.status, r.error, r.parser_version, r.sha256,
            (SELECT max(revision_no) FROM document_revisions x WHERE x.document_id=d.id AND x.status='confirmed') AS confirmed_revision
            FROM documents d JOIN document_revisions r ON r.document_id=d.id
            WHERE d.kb_id=? AND d.deleted_at IS NULL
              AND r.revision_no=(SELECT max(revision_no) FROM document_revisions y WHERE y.document_id=d.id)
            ORDER BY d.filename""", (kid,))
        gens = db.all_rows(conn, """SELECT id,status,embedding_model,embedding_connection,graph_status,created_at,activated_at,error,chunk_count
            FROM index_generations WHERE kb_id=? ORDER BY created_at DESC LIMIT 10""", (kid,))
        kb = db.one(conn, "SELECT active_generation FROM knowledge_bases WHERE id=?", (kid,))
    return {"documents": docs, "generations": gens, "active_generation": kb["active_generation"]}


@router.post("/knowledge-bases/{kid}/documents", status_code=201)
async def upload(kid: str, file: UploadFile = File(...), classification: str = Form("Internal"),
                 replaces: str = Form(""), actor: Actor = CurrentActor):
    if classification not in LEVEL:
        raise HTTPException(422, "Unknown classification")
    name = Path(file.filename or "upload").name[:200]
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(415, "Only PDF, DOCX, TXT and Markdown files are supported")
    limit = settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File is larger than {settings().max_upload_mb} MB")
    if not data:
        raise HTTPException(400, "The file is empty")
    digest = hashlib.sha256(data).hexdigest()
    with db.tx() as conn:
        kb = kb_for(conn, actor, kid, "write")
        if settings().public_mode and not actor.is_admin:
            docs = conn.execute("SELECT count(*) FROM documents WHERE created_by=? AND deleted_at IS NULL", (actor.id,)).fetchone()[0]
            used = conn.execute("SELECT coalesce(sum(amount),0) FROM usage_events WHERE user_id=? AND kind='upload_bytes'",
                                (actor.id,)).fetchone()[0]
            if docs >= settings().quota_docs_per_user or used + len(data) > settings().quota_upload_mb_per_user * 2**20:
                raise HTTPException(413, f"Storage limit reached ({settings().quota_docs_per_user} documents / "
                                         f"{settings().quota_upload_mb_per_user} MB per user).")
            conn.execute("INSERT INTO usage_events(user_id,kind,amount,created_at) VALUES(?,?,?,?)",
                         (actor.id, "upload_bytes", len(data), db.now()))
        cls = classification if LEVEL[classification] >= LEVEL[kb["classification"]] else kb["classification"]
        if replaces:
            doc = db.one(conn, "SELECT * FROM documents WHERE id=? AND kb_id=? AND deleted_at IS NULL", (replaces, kid))
            if not doc:
                raise HTTPException(404, "Document to replace not found")
            doc_id = doc["id"]
            rev_no = conn.execute("SELECT max(revision_no)+1 FROM document_revisions WHERE document_id=?",
                                  (doc_id,)).fetchone()[0]
            conn.execute("UPDATE documents SET classification=? WHERE id=?",
                         (cls if LEVEL[cls] >= LEVEL[doc["classification"]] else doc["classification"], doc_id))
        else:
            doc_id, rev_no = db.new_id(), 1
            conn.execute("INSERT INTO documents(id,kb_id,family_id,filename,classification,created_by,created_at) "
                         "VALUES(?,?,?,?,?,?,?)", (doc_id, kid, db.new_id(), name, cls, actor.id, db.now()))
        rid = db.new_id()
        folder = settings().uploads_dir / kid / doc_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rev{rev_no}{ext}"
        path.write_bytes(data)
        conn.execute("""INSERT INTO document_revisions(id,document_id,revision_no,sha256,stored_path,parser_version,status,
            created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                     (rid, doc_id, rev_no, digest, str(path), "", "uploaded", actor.id, db.now()))
        jobs.enqueue(conn, "document.parse", {"revision_id": rid}, idempotency_key="parse:" + rid)
        db.audit(conn, actor.id, "document.upload", "document", doc_id,
                 {"filename": name, "revision": rev_no, "sha256": digest, "classification": cls})
    return {"document_id": doc_id, "revision_id": rid, "revision_no": rev_no}


def _revision_for(conn, actor, rid, need="read"):
    rev = db.one(conn, """SELECT r.*, d.kb_id, d.filename FROM document_revisions r JOIN documents d ON d.id=r.document_id
        WHERE r.id=? AND d.deleted_at IS NULL""", (rid,))
    if not rev:
        raise HTTPException(404, "Revision not found")
    kb_for(conn, actor, rev["kb_id"], need)
    return rev


@router.get("/revisions/{rid}/preview")
def preview(rid: str, actor: Actor = CurrentActor):
    with db.read() as conn:
        rev = _revision_for(conn, actor, rid)
        blocks = db.all_rows(conn, "SELECT * FROM preview_blocks WHERE revision_id=? ORDER BY seq", (rid,))
    for b in blocks:
        b["equipment_tags"] = json.loads(b["equipment_tags"])
    return {"revision": rev, "blocks": blocks, "section_types": SECTION_TYPES}


@router.put("/revisions/{rid}/blocks")
def edit_blocks(rid: str, body: BlocksIn, actor: Actor = CurrentActor):
    with db.tx() as conn:
        rev = _revision_for(conn, actor, rid, "write")
        if rev["status"] != "preview_ready":
            raise HTTPException(409, "Only a preview that has not been confirmed can be edited; upload a new revision instead")
        for heading, st in body.heading_types.items():
            if st not in SECTION_TYPES:
                raise HTTPException(422, f"Unknown section type {st}")
            conn.execute("UPDATE preview_blocks SET section_type=? WHERE revision_id=? AND heading=?", (st, rid, heading))
        for b in body.blocks:
            if b.section_type is not None:
                if b.section_type not in SECTION_TYPES:
                    raise HTTPException(422, f"Unknown section type {b.section_type}")
                conn.execute("UPDATE preview_blocks SET section_type=? WHERE id=? AND revision_id=?", (b.section_type, b.id, rid))
            if b.include is not None:
                conn.execute("UPDATE preview_blocks SET include=? WHERE id=? AND revision_id=?", (int(b.include), b.id, rid))
        db.audit(conn, actor.id, "document.preview_edited", "document_revision", rid,
                 {"blocks": len(body.blocks), "headings": len(body.heading_types)})
    return {"updated": True}


@router.post("/revisions/{rid}/confirm")
def confirm(rid: str, actor: Actor = CurrentActor):
    with db.tx() as conn:
        rev = _revision_for(conn, actor, rid, "write")
        if rev["status"] != "preview_ready":
            raise HTTPException(409, f"This revision cannot be confirmed (status: {rev['status']})")
        conn.execute("UPDATE document_revisions SET status='confirmed', confirmed_by=?, confirmed_at=? WHERE id=?",
                     (actor.id, db.now(), rid))
        db.audit(conn, actor.id, "document.preview_confirmed", "document_revision", rid, {})
    return {"confirmed": True}


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: str, actor: Actor = CurrentActor):
    with db.tx() as conn:
        doc = db.one(conn, "SELECT * FROM documents WHERE id=? AND deleted_at IS NULL", (doc_id,))
        if not doc:
            raise HTTPException(404, "Document not found")
        kb_for(conn, actor, doc["kb_id"], "write")
        conn.execute("UPDATE documents SET deleted_at=? WHERE id=?", (db.now(), doc_id))   # retrieval stops immediately
        affected = flag_document_deletion(conn, doc_id, actor.id)
        emb = db.get_setting(conn, "embedding_model", settings().default_embedding_model)
        jobs.enqueue(conn, "kb.index", {"kb_id": doc["kb_id"], "actor_id": actor.id, "embedding_model": emb})
        db.audit(conn, actor.id, "document.delete", "document", doc_id, {"restricted_dataset_versions": affected})
    return {"deleted": True, "restricted_dataset_versions": affected}


@router.post("/knowledge-bases/{kid}/index")
def reindex(kid: str, actor: Actor = CurrentActor):
    with db.tx() as conn:
        kb_for(conn, actor, kid, "write")
        if conn.execute("SELECT 1 FROM index_generations WHERE kb_id=? AND status='building' AND created_at>?",
                        (kid, db.now() - 600)).fetchone():
            raise HTTPException(409, "Indexing is already running for this knowledge base")
        if not conn.execute("""SELECT 1 FROM document_revisions r JOIN documents d ON d.id=r.document_id
                WHERE d.kb_id=? AND d.deleted_at IS NULL AND r.status='confirmed'""", (kid,)).fetchone():
            raise HTTPException(400, "Confirm at least one document preview before indexing")
        emb = db.get_setting(conn, "embedding_model", settings().default_embedding_model)
        jid = jobs.enqueue(conn, "kb.index", {"kb_id": kid, "actor_id": actor.id, "embedding_model": emb})
        db.audit(conn, actor.id, "kb.index_requested", "knowledge_base", kid, {"embedding_model": emb})
    return {"job_id": jid}


@router.get("/sources/{chunk_id}")
def source_view(chunk_id: str, actor: Actor = CurrentActor):
    """Source viewer: only for chunks in the active generation of a readable KB of a live document."""
    with db.read() as conn:
        row = db.one(conn, """SELECT c.* FROM chunks c JOIN knowledge_bases b ON b.id=c.kb_id AND b.active_generation=c.generation_id
            JOIN documents d ON d.id=c.document_id AND d.deleted_at IS NULL WHERE c.id=?""", (chunk_id,))
        if not row:
            raise HTTPException(404, "Source not found or no longer available")
        kb_for(conn, actor, row["kb_id"])
    row.pop("embedding", None)
    row["equipment_tags"] = json.loads(row["equipment_tags"])
    return row
