"""Indexing (atomic index generations) and permission-aware hybrid retrieval.

Authorization is part of both the keyword (FTS5/BM25) and vector SQL queries:
only chunks of the *active* generation of knowledge bases the caller may read,
from documents that are not deleted, are ever loaded. Results are fused with
reciprocal rank fusion (RRF, k=60).
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, asdict

import numpy as np

from . import db
from .documents import chunk_blocks
from .llm import Gateway
from .policy import max_class

RRF_K = 60


# ------------------------------------------------------------------ indexing
def build_generation(kb_id: str, actor_id: str, embedding_model: str | None = None) -> dict:
    """Build a new generation from each document's latest confirmed revision, then swap atomically.
    On any failure the previous generation stays active."""
    from . import jobs
    gen_id = db.new_id()
    with db.tx() as conn:
        kb = db.one(conn, "SELECT * FROM knowledge_bases WHERE id=?", (kb_id,))
        if not kb:
            raise ValueError("knowledge base not found")
        busy = conn.execute("SELECT created_at FROM index_generations WHERE kb_id=? AND status='building'",
                            (kb_id,)).fetchone()
        if busy and db.now() - busy[0] < jobs.LEASE_S:
            raise jobs.Defer(20)                                   # one build per knowledge base at a time
        conn.execute("UPDATE index_generations SET status='failed', error='interrupted (server restart)' "
                     "WHERE kb_id=? AND status='building'", (kb_id,))
        emb_conn = kb.get("embedding_connection") or "local-ollama"
        embedding_model = kb.get("embedding_model") or embedding_model or db.get_setting(conn, "embedding_model", "")
        emb_owner = kb.get("created_by")
        conn.execute("""INSERT INTO index_generations(id,kb_id,status,embedding_model,created_by,created_at,
            embedding_connection,embedding_owner) VALUES(?,?,?,?,?,?,?,?)""",
                     (gen_id, kb_id, "building", embedding_model, actor_id, db.now(), emb_conn, emb_owner))
        revisions = db.all_rows(conn, """SELECT r.*, d.filename, d.classification FROM document_revisions r
            JOIN documents d ON d.id=r.document_id
            WHERE d.kb_id=? AND d.deleted_at IS NULL AND r.status='confirmed'
              AND r.revision_no=(SELECT max(revision_no) FROM document_revisions r2
                                 WHERE r2.document_id=r.document_id AND r2.status='confirmed')""", (kb_id,))
        blocks_by_rev = {r["id"]: db.all_rows(conn, "SELECT * FROM preview_blocks WHERE revision_id=? ORDER BY seq",
                                              (r["id"],)) for r in revisions}
    try:
        rows = []
        for rev in revisions:
            blocks = [dict(b, equipment_tags=json.loads(b["equipment_tags"])) for b in blocks_by_rev[rev["id"]]]
            for seq, ch in enumerate(chunk_blocks(blocks)):
                rows.append({"id": db.new_id(), "rev": rev, "seq": seq, "chunk": ch,
                             "classification": max_class(kb["classification"], rev["classification"])})
        gateway = Gateway(emb_conn, owner=emb_owner)
        vectors: list[list[float]] = []
        for i in range(0, len(rows), 32):
            batch = rows[i:i + 32]
            texts = [f"{r['chunk']['heading']}\n{r['chunk']['text']}" for r in batch]
            vectors.extend(gateway.embed(texts, embedding_model, max_class(*(r["classification"] for r in batch))))
        with db.tx() as conn:
            for r, vec in zip(rows, vectors):  # noqa: B007
                v = np.asarray(vec, dtype=np.float32)
                norm = float(np.linalg.norm(v)) or 1.0
                ch, rev = r["chunk"], r["rev"]
                cur = conn.execute("""INSERT INTO chunks(id,generation_id,kb_id,document_id,revision_id,revision_no,filename,
                    seq,section,section_type,location,text,equipment_tags,classification,embedding,dim)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r["id"], gen_id, kb_id, rev["document_id"], rev["id"], rev["revision_no"], rev["filename"],
                     r["seq"], ch["heading"], ch["section_type"], ch["location"], ch["text"],
                     json.dumps(ch["equipment_tags"]), r["classification"], (v / norm).tobytes(), len(v)))
                conn.execute("INSERT INTO chunks_fts(rowid,text) VALUES(?,?)",
                             (cur.lastrowid, f"{ch['heading']} {ch['text']}"))
        graph_note = None
        if kb.get("graph_mode", "off") != "off":
            from .graphrag import build_graph
            try:
                graph_note = build_graph(gen_id, kb)
            except Exception as exc:  # noqa: BLE001 - search still works without the graph
                graph_note = f"failed: {exc}"[:500]
        with db.tx() as conn:
            conn.execute("UPDATE index_generations SET graph_status=? WHERE id=?", (graph_note, gen_id))
            # Atomic swap: activation and supersession happen in one transaction.
            conn.execute("UPDATE index_generations SET status='superseded' WHERE kb_id=? AND status='ready'", (kb_id,))
            conn.execute("UPDATE index_generations SET status='ready', activated_at=?, chunk_count=? WHERE id=?",
                         (db.now(), len(rows), gen_id))
            conn.execute("UPDATE knowledge_bases SET active_generation=? WHERE id=?", (gen_id, kb_id))
            db.audit(conn, actor_id, "kb.index_activated", "knowledge_base", kb_id,
                     {"generation": gen_id, "chunks": len(rows), "embedding_model": embedding_model})
        _cleanup_old_generations(kb_id, keep=gen_id)
        return {"generation": gen_id, "chunks": len(rows)}
    except Exception as exc:
        with db.tx() as conn:
            conn.execute("UPDATE index_generations SET status='failed', error=? WHERE id=?", (str(exc)[:500], gen_id))
            rids = [r[0] for r in conn.execute("SELECT rid FROM chunks WHERE generation_id=?", (gen_id,))]
            conn.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(x,) for x in rids])
            conn.execute("DELETE FROM chunks WHERE generation_id=?", (gen_id,))
        raise


def _cleanup_old_generations(kb_id: str, keep: str) -> None:
    with db.tx() as conn:
        old = [r[0] for r in conn.execute(
            "SELECT id FROM index_generations WHERE kb_id=? AND status='superseded' AND id<>?", (kb_id, keep))]
        for gid in old:
            rids = [r[0] for r in conn.execute("SELECT rid FROM chunks WHERE generation_id=?", (gid,))]
            conn.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(x,) for x in rids])
            conn.execute("DELETE FROM chunks WHERE generation_id=?", (gid,))
            for t in ("graph_mentions", "graph_relations", "graph_communities", "graph_entities"):
                conn.execute(f"DELETE FROM {t} WHERE generation_id=?", (gid,))


# ------------------------------------------------------------------ scope
_EXPLICIT = {
    "startup": ("startup", "start-up", "start up", "commission"),
    "shutdown": ("shutdown", "shut down", "shut-down"),
    "interlock": ("interlock", "trip", "esd", "setpoint", "set point", "alarm"),
    "troubleshooting": ("troubleshoot", "problem", "fault", "why is", "cause", "abnormal"),
    "equipment_spec": ("specification", "design", "rating", "capacity", "data sheet", "datasheet"),
}
_VALUE_WORDS = ("pressure", "temperature", "flow rate", "flowrate", "value", "size", "level", "how much", "kitna")
_DESCRIPTIVE = ("describe", "description", "explain", "overview", "process flow", "how does", "what happens",
                "working of", "process of", "flow of", "समझाइए", "बताइए", "प्रक्रिया", "kaise")


def scope_for_question(question: str) -> tuple[list[str] | None, str]:
    q = question.lower()
    explicit = [t for t, words in _EXPLICIT.items() if any(w in q for w in words)]
    descriptive = any(w in q for w in _DESCRIPTIVE)
    if not explicit and not descriptive and any(w in q for w in _VALUE_WORDS):
        explicit = ["equipment_spec"]
    if explicit:
        allowed = sorted(set(explicit) | {"process_description", "equipment_spec"})
        return allowed, "question explicitly mentions: " + ", ".join(explicit)
    if descriptive:
        return ["process_description"], "process-description question: SOP/interlock sections excluded"
    return None, "general question: all section types"


# ------------------------------------------------------------------ retrieval
@dataclass
class EvidenceItem:
    ref: int
    chunk_id: str
    kb_id: str
    document_id: str
    filename: str
    revision_no: int
    section: str
    section_type: str
    location: str
    text: str
    equipment_tags: list[str]
    classification: str
    score: float

    def public(self) -> dict:
        return asdict(self)


def _fts_query(question: str) -> str:
    tokens = re.findall(r"[\w\-\.]+", question, flags=re.UNICODE)
    tokens = [t.replace('"', "") for t in tokens if len(t) > 1][:24]
    return " OR ".join(f'"{t}"' for t in tokens)


def authorized_filter(kb_ids: set[str]) -> tuple[str, list]:
    marks = ",".join("?" * len(kb_ids))
    sql = (f"c.kb_id IN ({marks}) AND c.generation_id=(SELECT active_generation FROM knowledge_bases WHERE id=c.kb_id) "
           f"AND EXISTS(SELECT 1 FROM documents d WHERE d.id=c.document_id AND d.deleted_at IS NULL)")
    return sql, list(kb_ids)


def retrieve(conn: sqlite3.Connection, question: str, kb_ids: set[str], *, top_k: int = 6,
             section_types: list[str] | None = None, question_vector: list[float] | None = None,
             vectors_by_kb: dict[str, list[float]] | None = None, graph: str = "off") -> list[EvidenceItem]:
    """kb_ids MUST already be intersected with the caller's current grants.
    graph: off | local | global | both  (GraphRAG, only where the KB has a graph)."""
    if not kb_ids or not question.strip():
        return []
    where, params = authorized_filter(kb_ids)
    if section_types:
        where += f" AND c.section_type IN ({','.join('?' * len(section_types))})"
        params += list(section_types)
    ranks: dict[str, float] = {}
    rows: dict[str, dict] = {}

    def add(ranked_rows):
        for rank, r in enumerate(ranked_rows, start=1):
            ranks[r["id"]] = ranks.get(r["id"], 0) + 1.0 / (RRF_K + rank)
            rows[r["id"]] = r

    q = _fts_query(question)
    if q:
        add(db.all_rows(conn, f"""SELECT c.*, bm25(chunks_fts) AS s FROM chunks_fts
            JOIN chunks c ON c.rid=chunks_fts.rowid WHERE chunks_fts MATCH ? AND {where}
            ORDER BY s LIMIT 50""", [q] + params))
    vectors = dict(vectors_by_kb or {})
    if question_vector is not None:
        vectors.update({k: question_vector for k in kb_ids if k not in vectors})
    if vectors:
        cand = db.all_rows(conn, f"SELECT c.* FROM chunks c WHERE {where} AND c.embedding IS NOT NULL", params)
        scored = []
        for kb_id, vec in vectors.items():
            qv = np.asarray(vec, dtype=np.float32)
            qv /= float(np.linalg.norm(qv)) or 1.0
            same = [r for r in cand if r["kb_id"] == kb_id and r["dim"] == len(qv)]
            if same:
                mat = np.frombuffer(b"".join(r["embedding"] for r in same), dtype=np.float32).reshape(len(same), -1)
                scored += list(zip((mat @ qv).tolist(), same))
        scored.sort(key=lambda x: -x[0])
        add([r for _, r in scored[:50]])
    extra: list[EvidenceItem] = []
    if graph != "off":
        from .graphrag import global_summaries, local_chunk_ranking
        gens = [r[0] for r in conn.execute(
            f"SELECT active_generation FROM knowledge_bases WHERE id IN ({','.join('?' * len(kb_ids))}) "
            "AND active_generation IS NOT NULL", list(kb_ids))]
        if graph in ("local", "both"):
            chunk_ids, _ = local_chunk_ranking(conn, question, gens)
            if chunk_ids:
                marks = ",".join("?" * len(chunk_ids))
                found = {r["id"]: r for r in db.all_rows(conn, f"SELECT c.* FROM chunks c WHERE c.id IN ({marks}) AND {where}",
                                                          chunk_ids + params)}
                add([found[c] for c in chunk_ids if c in found])
        if graph in ("global", "both"):
            for s_row in global_summaries(conn, question, gens, vectors):
                cls = conn.execute("SELECT classification FROM knowledge_bases WHERE id=?", (s_row["kb_id"],)).fetchone()[0]
                extra.append(EvidenceItem(0, f"community:{s_row['generation_id']}:{s_row['community']}", s_row["kb_id"],
                                          f"graph:{s_row['kb_id']}", "Knowledge-graph summary (generated)", 0,
                                          s_row["title"], "graph_summary", f"community {s_row['community']}",
                                          s_row["summary"], [], cls, 0.0))
    best = sorted(ranks.items(), key=lambda kv: (-kv[1], kv[0]))[:max(1, top_k - len(extra))]
    out = []
    for cid, score in best:
        r = rows[cid]
        out.append(EvidenceItem(0, r["id"], r["kb_id"], r["document_id"], r["filename"], r["revision_no"],
                                r["section"], r["section_type"], r["location"], r["text"],
                                json.loads(r["equipment_tags"]), r["classification"], round(score, 6)))
    out += extra
    for i, e in enumerate(out, start=1):
        e.ref = i
    return out


def still_accessible(conn: sqlite3.Connection, chunk_ids: list[str], kb_ids: set[str]) -> set[str]:
    """Final release re-check: which of these chunks are still readable right now."""
    if not chunk_ids or not kb_ids:
        return set()
    where, params = authorized_filter(kb_ids)
    plain = [c for c in chunk_ids if not c.startswith("community:")]
    ok: set[str] = set()
    if plain:
        marks = ",".join("?" * len(plain))
        ok = {r[0] for r in conn.execute(f"SELECT c.id FROM chunks c WHERE c.id IN ({marks}) AND {where}", plain + params)}
    active = {r[0] for r in conn.execute(
        f"SELECT active_generation FROM knowledge_bases WHERE id IN ({','.join('?' * len(kb_ids))})", list(kb_ids))}
    ok |= {c for c in chunk_ids if c.startswith("community:") and c.split(":")[1] in active}
    return ok
