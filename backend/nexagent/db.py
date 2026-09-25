"""SQLite storage (WAL mode) with versioned, forward-only migrations.

One connection per unit of work; `tx()` gives an IMMEDIATE transaction so
read-check-write sequences (last admin, last owner, index activation) are
serialised. All authorization decisions are made in SQL inside the same
transaction that performs the change.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings

SCHEMA: list[str] = [
# ---- v1: identities, projects, knowledge ---------------------------------
"""
CREATE TABLE users(
  id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL);
CREATE TABLE user_roles(
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('Admin','Builder','Reviewer','TrainingOperator','Viewer')),
  PRIMARY KEY(user_id, role));
CREATE TABLE user_permissions(
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  permission TEXT NOT NULL CHECK(permission IN ('model.promote')),
  PRIMARY KEY(user_id, permission));
CREATE TABLE sessions(
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE INDEX sessions_user ON sessions(user_id);
CREATE TABLE login_failures(
  key TEXT PRIMARY KEY, failures INTEGER NOT NULL, locked_until REAL NOT NULL, last_failure REAL NOT NULL);
CREATE TABLE projects(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  classification_floor TEXT NOT NULL CHECK(classification_floor IN ('Public','Internal','Restricted')),
  created_by TEXT NOT NULL REFERENCES users(id), created_at REAL NOT NULL);
CREATE TABLE project_members(
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  membership TEXT NOT NULL CHECK(membership IN ('owner','editor','viewer')),
  PRIMARY KEY(project_id, user_id));
CREATE TABLE knowledge_bases(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  classification TEXT NOT NULL CHECK(classification IN ('Public','Internal','Restricted')),
  active_generation TEXT, created_at REAL NOT NULL, UNIQUE(project_id, name));
CREATE TABLE kb_grants(
  kb_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  can_read INTEGER NOT NULL DEFAULT 1, can_write INTEGER NOT NULL DEFAULT 0,
  CHECK(can_write = 0 OR can_read = 1), PRIMARY KEY(kb_id, user_id));
CREATE TRIGGER revoke_member_grants AFTER DELETE ON project_members BEGIN
  DELETE FROM kb_grants WHERE user_id = OLD.user_id
    AND kb_id IN (SELECT id FROM knowledge_bases WHERE project_id = OLD.project_id);
END;
CREATE TABLE documents(
  id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  family_id TEXT NOT NULL, filename TEXT NOT NULL,
  classification TEXT NOT NULL CHECK(classification IN ('Public','Internal','Restricted')),
  created_by TEXT NOT NULL, created_at REAL NOT NULL, deleted_at REAL);
CREATE TABLE document_revisions(
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  revision_no INTEGER NOT NULL, sha256 TEXT NOT NULL, stored_path TEXT NOT NULL,
  parser_version TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
  created_by TEXT NOT NULL, created_at REAL NOT NULL, confirmed_by TEXT, confirmed_at REAL,
  UNIQUE(document_id, revision_no));
CREATE TABLE preview_blocks(
  id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES document_revisions(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, heading TEXT NOT NULL, section_type TEXT NOT NULL, kind TEXT NOT NULL,
  text TEXT NOT NULL, location TEXT NOT NULL, equipment_tags TEXT NOT NULL DEFAULT '[]',
  include INTEGER NOT NULL DEFAULT 1);
CREATE INDEX preview_rev ON preview_blocks(revision_id, seq);
CREATE TABLE index_generations(
  id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status IN ('building','ready','failed','superseded')),
  embedding_model TEXT NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL,
  activated_at REAL, error TEXT, chunk_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE chunks(
  rid INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
  generation_id TEXT NOT NULL REFERENCES index_generations(id) ON DELETE CASCADE,
  kb_id TEXT NOT NULL, document_id TEXT NOT NULL, revision_id TEXT NOT NULL, revision_no INTEGER NOT NULL,
  filename TEXT NOT NULL, seq INTEGER NOT NULL, section TEXT NOT NULL, section_type TEXT NOT NULL,
  location TEXT NOT NULL, text TEXT NOT NULL, equipment_tags TEXT NOT NULL,
  classification TEXT NOT NULL, embedding BLOB, dim INTEGER NOT NULL DEFAULT 0);
CREATE INDEX chunks_gen ON chunks(generation_id);
CREATE VIRTUAL TABLE chunks_fts USING fts5(text, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE audit_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT, action TEXT NOT NULL,
  resource_type TEXT NOT NULL, resource_id TEXT, details TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
""",
# ---- v2: models, workflows, runs, jobs, publishing ------------------------
"""
CREATE TABLE model_connections(
  id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('ollama','openai')),
  base_url TEXT NOT NULL, max_classification TEXT NOT NULL CHECK(max_classification IN ('Public','Internal','Restricted')),
  external INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
  api_key_enc TEXT, created_at REAL NOT NULL);
CREATE TABLE app_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE workflows(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL,
  created_at REAL NOT NULL, latest_version INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE workflow_versions(
  id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  version_no INTEGER NOT NULL, graph_json TEXT NOT NULL, valid INTEGER NOT NULL,
  validation_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL,
  UNIQUE(workflow_id, version_no));
CREATE TABLE assistants(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  workflow_id TEXT NOT NULL REFERENCES workflows(id), slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  version_id TEXT REFERENCES workflow_versions(id), previous_version_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('published','unpublished')), created_by TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE api_keys(
  id TEXT PRIMARY KEY, assistant_id TEXT NOT NULL REFERENCES assistants(id) ON DELETE CASCADE,
  name TEXT NOT NULL, prefix TEXT UNIQUE NOT NULL, verifier TEXT NOT NULL,
  max_classification TEXT NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL);
CREATE TABLE api_key_kbs(
  key_id TEXT NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE, kb_id TEXT NOT NULL,
  PRIMARY KEY(key_id, kb_id));
CREATE TABLE runs(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workflow_version_id TEXT NOT NULL,
  assistant_id TEXT, user_id TEXT, api_key_id TEXT, kind TEXT NOT NULL CHECK(kind IN ('test','chat','api','eval')),
  question TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','running','awaiting_review','completed','not_found','failed','cancelled')),
  cancel_requested INTEGER NOT NULL DEFAULT 0, answer_json TEXT, pinned_json TEXT, classification TEXT NOT NULL DEFAULT 'Internal',
  error TEXT, created_at REAL NOT NULL, finished_at REAL);
CREATE INDEX runs_user ON runs(user_id, created_at);
CREATE TABLE run_nodes(
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, node_id TEXT NOT NULL, node_type TEXT NOT NULL,
  status TEXT NOT NULL, started_at REAL, finished_at REAL, duration_ms INTEGER, summary TEXT, detail TEXT, error TEXT,
  PRIMARY KEY(run_id, node_id));
CREATE TABLE run_events(
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, seq INTEGER NOT NULL, type TEXT NOT NULL,
  data TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(run_id, seq));
CREATE TABLE jobs(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, queue TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL CHECK(status IN ('queued','leased','done','failed')),
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
  lease_until REAL, worker TEXT, idempotency_key TEXT UNIQUE, error TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, run_after REAL NOT NULL DEFAULT 0);
CREATE INDEX jobs_ready ON jobs(queue, status, run_after);
""",
# ---- v3: review, feedback, datasets, training, deployments ---------------
"""
CREATE TABLE review_items(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), project_id TEXT NOT NULL,
  question TEXT NOT NULL, draft TEXT NOT NULL, evidence_json TEXT NOT NULL, findings_json TEXT NOT NULL,
  classification TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','approved','corrected','rejected')),
  assigned_to TEXT, created_at REAL NOT NULL, closed_at REAL);
CREATE TABLE review_decisions(
  id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES review_items(id),
  reviewer_id TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('approve','correct','reject')),
  answer TEXT, citations_json TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TRIGGER decisions_immutable BEFORE UPDATE ON review_decisions BEGIN SELECT RAISE(ABORT,'decisions are immutable'); END;
CREATE TABLE feedback(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), user_id TEXT NOT NULL,
  rating INTEGER NOT NULL CHECK(rating IN (-1,1)), comment TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
  UNIQUE(run_id, user_id));
CREATE TABLE dataset_candidates(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, decision_id TEXT UNIQUE NOT NULL REFERENCES review_decisions(id),
  question TEXT NOT NULL, answer TEXT NOT NULL, evidence_json TEXT NOT NULL, source_chunk_ids TEXT NOT NULL,
  source_document_ids TEXT NOT NULL, group_id TEXT NOT NULL, classification TEXT NOT NULL,
  answer_reviewer TEXT NOT NULL, content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('proposed','approved','excluded')), created_at REAL NOT NULL);
CREATE TABLE dataset_approvals(
  id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES dataset_candidates(id),
  content_hash TEXT NOT NULL, approver_id TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('include','exclude')),
  note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE dataset_versions(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL,
  manifest_json TEXT NOT NULL, sha256 TEXT NOT NULL, classification TEXT NOT NULL,
  counts_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL,
  restricted_reason TEXT, UNIQUE(project_id, name, version));
CREATE TRIGGER dataset_immutable BEFORE UPDATE OF manifest_json, sha256 ON dataset_versions
BEGIN SELECT RAISE(ABORT,'dataset versions are immutable'); END;
CREATE TABLE base_models(
  id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, path TEXT NOT NULL, license TEXT NOT NULL,
  license_accepted_by TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE training_jobs(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, dataset_version_id TEXT NOT NULL REFERENCES dataset_versions(id),
  base_model_id TEXT NOT NULL REFERENCES base_models(id), recipe TEXT NOT NULL, params_json TEXT NOT NULL,
  seed INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending_approval','blocked','queued','running','cancelling','cancelled','failed','completed','rejected')),
  requested_by TEXT NOT NULL, approved_by TEXT, approved_at REAL, preflight_json TEXT, software_json TEXT,
  hardware_json TEXT, output_dir TEXT, pid INTEGER, error TEXT, created_at REAL NOT NULL,
  started_at REAL, finished_at REAL);
CREATE TABLE training_metrics(
  job_id TEXT NOT NULL REFERENCES training_jobs(id) ON DELETE CASCADE, step INTEGER NOT NULL,
  epoch REAL, loss REAL, eval_loss REAL, learning_rate REAL, created_at REAL NOT NULL,
  PRIMARY KEY(job_id, step));
CREATE TABLE model_artifacts(
  id TEXT PRIMARY KEY, training_job_id TEXT NOT NULL REFERENCES training_jobs(id), kind TEXT NOT NULL,
  path TEXT NOT NULL, files_json TEXT NOT NULL, classification TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('registered','restricted','retired')), created_at REAL NOT NULL);
CREATE TABLE evaluations(
  id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES model_artifacts(id),
  dataset_version_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('queued','running','failed','completed')),
  results_json TEXT, error TEXT, requested_by TEXT NOT NULL, created_at REAL NOT NULL, finished_at REAL);
CREATE TABLE deployments(
  id TEXT PRIMARY KEY, alias TEXT NOT NULL, artifact_id TEXT NOT NULL REFERENCES model_artifacts(id),
  evaluation_id TEXT NOT NULL REFERENCES evaluations(id), ollama_model TEXT,
  status TEXT NOT NULL CHECK(status IN ('pending_export','export_failed','active','superseded','rolled_back')),
  approved_by TEXT NOT NULL, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE model_aliases(
  alias TEXT PRIMARY KEY, deployment_id TEXT NOT NULL, previous_deployment_id TEXT, updated_at REAL NOT NULL);
""",
# ---- v4: public providers, personal connections, MCP, GraphRAG, public mode --
"""
CREATE TABLE model_connections_v4(
  id TEXT PRIMARY KEY, name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('ollama','openai','anthropic','gemini')),
  base_url TEXT NOT NULL, max_classification TEXT NOT NULL CHECK(max_classification IN ('Public','Internal','Restricted')),
  external INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, api_key_enc TEXT,
  owner_user_id TEXT REFERENCES users(id) ON DELETE CASCADE, created_at REAL NOT NULL);
INSERT INTO model_connections_v4(id,name,kind,base_url,max_classification,external,enabled,api_key_enc,owner_user_id,created_at)
  SELECT id,name,kind,base_url,max_classification,external,enabled,api_key_enc,NULL,created_at FROM model_connections;
DROP TABLE model_connections;
ALTER TABLE model_connections_v4 RENAME TO model_connections;
CREATE UNIQUE INDEX conn_shared_name ON model_connections(name) WHERE owner_user_id IS NULL;
CREATE UNIQUE INDEX conn_owner_name ON model_connections(owner_user_id, name) WHERE owner_user_id IS NOT NULL;
ALTER TABLE knowledge_bases ADD COLUMN created_by TEXT;
ALTER TABLE knowledge_bases ADD COLUMN embedding_connection TEXT;
ALTER TABLE knowledge_bases ADD COLUMN embedding_model TEXT;
ALTER TABLE knowledge_bases ADD COLUMN graph_mode TEXT NOT NULL DEFAULT 'off';
ALTER TABLE knowledge_bases ADD COLUMN graph_connection TEXT;
ALTER TABLE knowledge_bases ADD COLUMN graph_model TEXT;
ALTER TABLE index_generations ADD COLUMN embedding_connection TEXT;
ALTER TABLE index_generations ADD COLUMN embedding_owner TEXT;
ALTER TABLE index_generations ADD COLUMN graph_status TEXT;
ALTER TABLE projects ADD COLUMN require_review INTEGER NOT NULL DEFAULT 1;
ALTER TABLE users ADD COLUMN email TEXT;
ALTER TABLE users ADD COLUMN self_registered INTEGER NOT NULL DEFAULT 0;
CREATE TABLE mcp_servers(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, transport TEXT NOT NULL CHECK(transport IN ('http','stdio')),
  url TEXT, command TEXT, args_json TEXT NOT NULL DEFAULT '[]', env_enc TEXT, headers_enc TEXT,
  max_classification TEXT NOT NULL CHECK(max_classification IN ('Public','Internal','Restricted')),
  owner_user_id TEXT REFERENCES users(id) ON DELETE CASCADE, enabled INTEGER NOT NULL DEFAULT 1,
  last_error TEXT, checked_at REAL, created_at REAL NOT NULL);
CREATE UNIQUE INDEX mcp_shared_name ON mcp_servers(name) WHERE owner_user_id IS NULL;
CREATE UNIQUE INDEX mcp_owner_name ON mcp_servers(owner_user_id, name) WHERE owner_user_id IS NOT NULL;
CREATE TABLE mcp_tools(
  server_id TEXT NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE, name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', schema_json TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(server_id, name));
CREATE TABLE graph_entities(
  id TEXT PRIMARY KEY, generation_id TEXT NOT NULL REFERENCES index_generations(id) ON DELETE CASCADE,
  kb_id TEXT NOT NULL, name TEXT NOT NULL, norm TEXT NOT NULL, type TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  community INTEGER, degree INTEGER NOT NULL DEFAULT 0);
CREATE INDEX graph_entities_gen ON graph_entities(generation_id, norm);
CREATE TABLE graph_relations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, generation_id TEXT NOT NULL REFERENCES index_generations(id) ON DELETE CASCADE,
  src TEXT NOT NULL, dst TEXT NOT NULL, relation TEXT NOT NULL, chunk_id TEXT, weight REAL NOT NULL DEFAULT 1);
CREATE INDEX graph_relations_gen ON graph_relations(generation_id, src);
CREATE TABLE graph_mentions(
  generation_id TEXT NOT NULL REFERENCES index_generations(id) ON DELETE CASCADE,
  entity_id TEXT NOT NULL, chunk_id TEXT NOT NULL, PRIMARY KEY(entity_id, chunk_id));
CREATE INDEX graph_mentions_chunk ON graph_mentions(chunk_id);
CREATE TABLE graph_communities(
  generation_id TEXT NOT NULL REFERENCES index_generations(id) ON DELETE CASCADE, community INTEGER NOT NULL,
  title TEXT NOT NULL, summary TEXT NOT NULL, entity_count INTEGER NOT NULL, summary_embedding BLOB,
  PRIMARY KEY(generation_id, community));
CREATE TABLE usage_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, kind TEXT NOT NULL, amount REAL NOT NULL DEFAULT 1,
  created_at REAL NOT NULL);
CREATE INDEX usage_user ON usage_events(user_id, kind, created_at);
""",
]

_local = threading.local()
_init_lock = threading.Lock()


def now() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex


def connect() -> sqlite3.Connection:
    path = settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def migrate() -> int:
    with _init_lock:
        conn = connect()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL)")
            row = conn.execute("SELECT max(version) v FROM schema_version").fetchone()
            current = row["v"] or 0
            for index, script in enumerate(SCHEMA, start=1):
                if index <= current:
                    continue
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _split(script):
                        conn.execute(statement)
                    conn.execute("INSERT INTO schema_version(version) VALUES(?)", (index,))
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            return len(SCHEMA)
        finally:
            conn.close()


def _split(script: str) -> list[str]:
    """Split a script into complete statements (trigger bodies stay intact)."""
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if buf.strip() and sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        raise RuntimeError("Incomplete SQL statement in migration")
    return out


@contextmanager
def tx(immediate: bool = True) -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()


@contextmanager
def read() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def one(conn: sqlite3.Connection, sql: str, params: Any = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def all_rows(conn: sqlite3.Connection, sql: str, params: Any = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def audit(conn: sqlite3.Connection, actor: str | None, action: str, resource_type: str,
          resource_id: str | None = None, details: dict | None = None) -> None:
    conn.execute("INSERT INTO audit_events(actor_id,action,resource_type,resource_id,details,created_at) VALUES(?,?,?,?,?,?)",
                 (actor, action, resource_type, resource_id, json.dumps(details or {}, ensure_ascii=False), now()))


def get_setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))
