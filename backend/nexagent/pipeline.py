"""Background handlers for document extraction and indexing."""
from __future__ import annotations

import json
from pathlib import Path

from . import db, jobs
from .documents import PARSER_VERSION, parse_isolated
from .retrieval import build_generation


@jobs.handler("document.parse")
def parse_revision(payload: dict) -> None:
    rid = payload["revision_id"]
    with db.read() as conn:
        rev = db.one(conn, "SELECT * FROM document_revisions WHERE id=?", (rid,))
    if not rev or rev["status"] != "uploaded":
        return
    result = parse_isolated(Path(rev["stored_path"]))
    with db.tx() as conn:
        conn.execute("DELETE FROM preview_blocks WHERE revision_id=?", (rid,))
        for seq, b in enumerate(result.get("blocks", [])):
            conn.execute("""INSERT INTO preview_blocks(id,revision_id,seq,heading,section_type,kind,text,location,equipment_tags)
                VALUES(?,?,?,?,?,?,?,?,?)""", (db.new_id(), rid, seq, b["heading"], b["section_type"], b["kind"],
                                                b["text"], b["location"], json.dumps(b["equipment_tags"])))
        status = result["status"] if result["status"] in ("preview_ready", "scanned_ocr_deferred") else "failed"
        if status == "preview_ready" and not result.get("blocks"):
            status, result["message"] = "failed", "No text could be extracted from this file."
        conn.execute("UPDATE document_revisions SET status=?, error=?, parser_version=? WHERE id=?",
                     (status, result.get("message"), result.get("parser_version", PARSER_VERSION), rid))


@jobs.handler("kb.index")
def index_kb(payload: dict) -> None:
    try:
        build_generation(payload["kb_id"], payload["actor_id"], payload["embedding_model"])
    except jobs.Defer:
        raise
    except Exception as exc:
        # previous generation stays active; the failed generation row records the reason
        raise jobs.PermanentError(f"Indexing failed; the previous index is still in use. {exc}") from exc


@jobs.handler("kb.index:failed")
def index_failed(payload: dict) -> None:
    with db.tx() as conn:   # a generation left 'building' by a crash must not block future indexing
        conn.execute("""UPDATE index_generations SET status='failed', error=coalesce(error, ?)
            WHERE kb_id=? AND status='building'""", (payload.get("error", "interrupted"), payload["kb_id"]))


@jobs.handler("document.parse:failed")
def parse_failed(payload: dict) -> None:
    with db.tx() as conn:
        conn.execute("UPDATE document_revisions SET status='failed', error=? WHERE id=? AND status='uploaded'",
                     (payload.get("error", "Parsing failed")[:300], payload["revision_id"]))
