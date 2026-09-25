"""Approved corrections → dataset candidates → immutable dataset versions.

Review point 6 fixes:
* Two-person rule: the dataset approver must be a different person from the
  engineer who approved/corrected the answer.
* The training export contains the train AND validation splits (the trainer
  needs validation loss), never the held-out split.
* The export carries the dataset hash and classification so the trained
  adapter inherits the restriction.
* A version needs at least one train and one held-out example.

A thumbs-up never creates a candidate; only an engineer's review decision does,
and even then inclusion needs a separate explicit approval.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict

from . import db
from .policy import max_class
from .validators import devanagari_share

SPLITS = ("train", "validation", "heldout")


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def candidate_hash(c: dict) -> str:
    return sha({k: c[k] for k in ("question", "answer", "source_chunk_ids", "source_document_ids", "group_id",
                                  "classification", "evidence")})


def norm_question(q: str) -> str:
    return " ".join(re.findall(r"\w+", q.casefold()))


# ------------------------------------------------------------------ candidates
def create_candidate(conn: sqlite3.Connection, decision_id: str) -> str | None:
    d = db.one(conn, """SELECT d.*, i.question, i.evidence_json, i.classification, i.project_id FROM review_decisions d
        JOIN review_items i ON i.id=d.item_id WHERE d.id=?""", (decision_id,))
    if not d or d["decision"] not in ("approve", "correct") or not (d["answer"] or "").strip():
        return None
    evidence = json.loads(d["evidence_json"])
    cited_refs = set(json.loads(d["citations_json"]) or [])
    used = [e for e in evidence if e["ref"] in cited_refs] or []
    doc_ids = sorted({e["document_id"] for e in used})
    fams = sorted({r[0] for r in conn.execute(
        f"SELECT family_id FROM documents WHERE id IN ({','.join('?' * len(doc_ids))})", doc_ids)}) if doc_ids else []
    c = {"question": d["question"], "answer": d["answer"], "evidence": [{k: e[k] for k in
         ("ref", "chunk_id", "document_id", "filename", "revision_no", "section", "section_type", "location", "text")}
         for e in used],
         "source_chunk_ids": sorted(e["chunk_id"] for e in used), "source_document_ids": doc_ids,
         "group_id": "+".join(fams) or "ungrouped:" + d["item_id"], "classification": d["classification"]}
    cid = db.new_id()
    conn.execute("""INSERT INTO dataset_candidates(id,project_id,decision_id,question,answer,evidence_json,
        source_chunk_ids,source_document_ids,group_id,classification,answer_reviewer,content_hash,status,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (cid, d["project_id"], decision_id, c["question"], c["answer"], canonical(c["evidence"]),
                  canonical(c["source_chunk_ids"]), canonical(c["source_document_ids"]), c["group_id"],
                  c["classification"], d["reviewer_id"], candidate_hash(c), "proposed", db.now()))
    return cid


def load_candidate(row: dict) -> dict:
    c = dict(row)
    c["evidence"] = json.loads(row["evidence_json"])
    c["source_chunk_ids"] = json.loads(row["source_chunk_ids"])
    c["source_document_ids"] = json.loads(row["source_document_ids"])
    return c


def edit_candidate(conn: sqlite3.Connection, cid: str, question: str, answer: str, actor_id: str) -> None:
    row = db.one(conn, "SELECT * FROM dataset_candidates WHERE id=?", (cid,))
    c = load_candidate(row)
    c["question"], c["answer"] = question, answer
    conn.execute("UPDATE dataset_candidates SET question=?, answer=?, content_hash=?, status='proposed' WHERE id=?",
                 (question, answer, candidate_hash(c), cid))      # old approvals no longer match the hash
    db.audit(conn, actor_id, "dataset.candidate_edited", "dataset_candidate", cid, {})


def approve_candidate(conn: sqlite3.Connection, cid: str, approver_id: str, decision: str, note: str = "") -> None:
    row = db.one(conn, "SELECT * FROM dataset_candidates WHERE id=?", (cid,))
    if row is None:
        raise ValueError("candidate_not_found")
    if decision == "include" and row["answer_reviewer"] == approver_id:
        raise ValueError("two_person_rule: the engineer who approved the answer cannot also approve it for training")
    if decision == "include" and not json.loads(row["source_chunk_ids"]):
        raise ValueError("missing_citations: an example without cited sources cannot be included")
    conn.execute("""INSERT INTO dataset_approvals(id,candidate_id,content_hash,approver_id,decision,note,created_at)
        VALUES(?,?,?,?,?,?,?)""", (db.new_id(), cid, row["content_hash"], approver_id, decision, note, db.now()))
    conn.execute("UPDATE dataset_candidates SET status=? WHERE id=?",
                 ("approved" if decision == "include" else "excluded", cid))
    db.audit(conn, approver_id, f"dataset.candidate_{decision}", "dataset_candidate", cid, {"note": note})


# ------------------------------------------------------------------ preparation assistant (rule-based)
SENSITIVE = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("phone", re.compile(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)")),
    ("ip_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("password", re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*\S+")),
    ("employee_id", re.compile(r"(?i)\b(emp(?:loyee)?\s*(?:no|id|code)\.?\s*[:#]?\s*\d{4,})")),
    ("pan_or_aadhaar", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b|\b\d{4}\s\d{4}\s\d{4}\b")),
]


def proposals(cands: list[dict]) -> list[dict]:
    """Suggestions only; a human decides. Labelled 'rule-based' in the UI."""
    out: list[dict] = []
    by_q = defaultdict(list)
    for c in cands:
        by_q[norm_question(c["question"])].append(c)
    for q, group in by_q.items():
        if len(group) > 1:
            answers = {norm_question(c["answer"]) for c in group}
            kind = "contradiction" if len(answers) > 1 else "duplicate"
            out.append({"kind": kind, "candidate_ids": [c["id"] for c in group],
                        "message": ("Same question with different answers — keep one." if kind == "contradiction"
                                    else "Duplicate example — keep one.")})
    # near-duplicates (token Jaccard ≥ 0.85) across different normalised questions
    toks = [(c, set(norm_question(c["question"]).split())) for c in cands]
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            a, b = toks[i][1], toks[j][1]
            if a and b and norm_question(toks[i][0]["question"]) != norm_question(toks[j][0]["question"]) \
                    and len(a & b) / len(a | b) >= 0.85:
                out.append({"kind": "near_duplicate", "candidate_ids": [toks[i][0]["id"], toks[j][0]["id"]],
                            "message": "Very similar questions — check they are not the same example."})
    for c in cands:
        for label, rx in SENSITIVE:
            if rx.search(c["question"]) or rx.search(c["answer"]):
                out.append({"kind": "sensitive_data", "candidate_ids": [c["id"]],
                            "message": f"Possible {label.replace('_', ' ')} — redact before inclusion."})
        if not c["source_chunk_ids"]:
            out.append({"kind": "missing_citations", "candidate_ids": [c["id"]], "message": "No cited source."})
        if len(c["answer"].strip()) < 20:
            out.append({"kind": "poor_example", "candidate_ids": [c["id"]], "message": "Answer is very short."})
        elif len(c["answer"]) > 4000:
            out.append({"kind": "poor_example", "candidate_ids": [c["id"]], "message": "Answer is very long."})
    if cands:
        hindi = sum(1 for c in cands if devanagari_share(c["question"]) > 0.3)
        share = hindi / len(cands)
        if len(cands) >= 10 and (share < 0.1 or share > 0.9):
            out.append({"kind": "language_imbalance", "candidate_ids": [],
                        "message": f"{hindi} of {len(cands)} examples are in Hindi — consider balancing languages."})
    return out


def propose_splits(group_ids: list[str], ratios=(0.8, 0.1, 0.1)) -> dict[str, str]:
    """Deterministic (hash-based) group → split proposal. Humans confirm or change it."""
    groups = sorted(set(group_ids), key=lambda g: hashlib.sha256(g.encode()).hexdigest())
    n = len(groups)
    out = {}
    for i, g in enumerate(groups):
        pos = (i + 0.5) / n
        out[g] = "train" if pos < ratios[0] else ("validation" if pos < ratios[0] + ratios[1] else "heldout")
    if n >= 2 and "heldout" not in out.values():
        out[groups[-1]] = "heldout"
    if n >= 2 and "train" not in out.values():
        out[groups[0]] = "train"
    return out


# ------------------------------------------------------------------ versions
def build_version(conn: sqlite3.Connection, project_id: str, name: str, version: str,
                  split_by_group: dict[str, str], actor_id: str) -> dict:
    if not name.strip() or not version.strip():
        raise ValueError("dataset_requires_name_and_version")
    rows = db.all_rows(conn, "SELECT * FROM dataset_candidates WHERE project_id=? ORDER BY id", (project_id,))
    included, exclusions = [], []
    for row in rows:
        c = load_candidate(row)
        appr = db.one(conn, """SELECT * FROM dataset_approvals WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1""",
                      (c["id"],))
        reason = None
        if not appr or appr["decision"] != "include":
            reason = "not_approved" if not appr else "excluded_by_approver"
        elif appr["content_hash"] != c["content_hash"] or candidate_hash(c) != c["content_hash"]:
            reason = "approval_content_changed"
        elif appr["approver_id"] == c["answer_reviewer"]:
            reason = "two_person_rule"
        elif not _sources_alive(conn, c["source_document_ids"]):
            reason = "source_deleted_or_revoked"
        if reason:
            exclusions.append({"candidate_id": c["id"], "reason": reason})
            continue
        split = split_by_group.get(c["group_id"])
        if split not in SPLITS:
            raise ValueError(f"explicit_group_split_required: group {c['group_id']}")
        included.append((c, appr, split))
    if not included:
        raise ValueError("no_approved_examples")
    seen: dict[str, str] = {}
    for c, _, split in included:
        key = norm_question(c["question"])
        if key in seen:
            raise ValueError(f"duplicate_or_conflicting_question: {c['question'][:80]}")
        seen[key] = split
    counts = Counter(split for _, _, split in included)
    if counts["train"] < 1 or counts["heldout"] < 1:
        raise ValueError(f"split_minimums: need ≥1 train and ≥1 held-out example (have {dict(counts)})")
    examples = [{"id": c["id"], "group_id": c["group_id"], "split": split, "question": c["question"],
                 "answer": c["answer"], "evidence": c["evidence"], "source_chunk_ids": c["source_chunk_ids"],
                 "source_document_ids": c["source_document_ids"], "classification": c["classification"],
                 "content_hash": c["content_hash"],
                 "approvals": {"answer_reviewer": c["answer_reviewer"], "dataset_approver": a["approver_id"],
                               "approval_id": a["id"]}}
                for c, a, split in included]
    classification = max_class(*(e["classification"] for e in examples))
    manifest = {"schema": 2, "project_id": project_id, "name": name, "version": version,
                "classification": classification,
                "split_definition": {"method": "explicit group assignment", "by_group": dict(sorted(split_by_group.items()))},
                "examples": examples, "exclusions": exclusions}
    content = canonical(manifest)
    digest = hashlib.sha256(content.encode()).hexdigest()
    vid = db.new_id()
    conn.execute("""INSERT INTO dataset_versions(id,project_id,name,version,manifest_json,sha256,classification,
        counts_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                 (vid, project_id, name, version, content, digest, classification, canonical(dict(counts)),
                  actor_id, db.now()))
    db.audit(conn, actor_id, "dataset.version_created", "dataset_version", vid,
             {"sha256": digest, "counts": dict(counts), "excluded": len(exclusions)})
    return {"id": vid, "sha256": digest, "counts": dict(counts), "exclusions": exclusions}


def _sources_alive(conn: sqlite3.Connection, doc_ids: list[str]) -> bool:
    if not doc_ids:
        return False
    n = conn.execute(f"SELECT count(*) FROM documents WHERE id IN ({','.join('?' * len(doc_ids))}) AND deleted_at IS NULL",
                     doc_ids).fetchone()[0]
    return n == len(doc_ids)


def verified_manifest(conn: sqlite3.Connection, version_id: str) -> dict:
    row = db.one(conn, "SELECT * FROM dataset_versions WHERE id=?", (version_id,))
    if not row:
        raise ValueError("dataset_version_not_found")
    if hashlib.sha256(row["manifest_json"].encode()).hexdigest() != row["sha256"]:
        raise ValueError("manifest_hash_mismatch")
    if row["restricted_reason"]:
        raise ValueError(f"dataset_restricted: {row['restricted_reason']}")
    return json.loads(row["manifest_json"])


def training_export(conn: sqlite3.Connection, version_id: str) -> dict:
    """What the trainer may see: train + validation rows only, with provenance header."""
    m = verified_manifest(conn, version_id)
    row = db.one(conn, "SELECT sha256 FROM dataset_versions WHERE id=?", (version_id,))
    doc_ids = sorted({d for e in m["examples"] for d in e["source_document_ids"]})
    if not _sources_alive(conn, doc_ids):
        raise ValueError("source_access_revoked: a source document was deleted after this version was built")
    strip = lambda e: {"id": e["id"], "question": e["question"], "answer": e["answer"], "evidence": e["evidence"]}
    return {"dataset_version_id": version_id, "dataset_sha256": row["sha256"], "classification": m["classification"],
            "train": [strip(e) for e in m["examples"] if e["split"] == "train"],
            "validation": [strip(e) for e in m["examples"] if e["split"] == "validation"]}


def heldout_set(conn: sqlite3.Connection, version_id: str) -> list[dict]:
    """Only for the evaluation step (never mounted into a training job)."""
    m = verified_manifest(conn, version_id)
    return [{"id": e["id"], "question": e["question"], "answer": e["answer"], "evidence": e["evidence"]}
            for e in m["examples"] if e["split"] == "heldout"]


def flag_document_deletion(conn: sqlite3.Connection, document_id: str, actor_id: str) -> list[str]:
    """Deleting a source restricts dataset versions and trained artifacts that used it.
    It does NOT remove what a model already learned — artifacts are restricted, not 'cleaned'."""
    affected = []
    for row in db.all_rows(conn, "SELECT id, manifest_json FROM dataset_versions WHERE restricted_reason IS NULL"):
        m = json.loads(row["manifest_json"])
        if any(document_id in e["source_document_ids"] for e in m["examples"]):
            conn.execute("UPDATE dataset_versions SET restricted_reason=? WHERE id=?",
                         (f"source document {document_id} deleted", row["id"]))
            affected.append(row["id"])
            conn.execute("""UPDATE model_artifacts SET status='restricted' WHERE status='registered' AND training_job_id IN
                (SELECT id FROM training_jobs WHERE dataset_version_id=?)""", (row["id"],))
    conn.execute("UPDATE dataset_candidates SET status='excluded' WHERE status<>'excluded' AND source_document_ids LIKE ?",
                 (f'%"{document_id}"%',))
    if affected:
        db.audit(conn, actor_id, "dataset.restricted_by_deletion", "document", document_id, {"versions": affected})
    return affected
