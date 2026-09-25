"""Durable background jobs stored in SQLite (no Redis needed).

* enqueue() is called inside the same transaction as the change that needs the
  job (transactional outbox), with an idempotency key.
* Workers lease jobs; a lease that expires (crash, restart) is picked up again.
* Retries are bounded (max_attempts) with back-off; failures are recorded.
* Queues: 'default' (indexing, runs, exports) and 'training' (one at a time).
"""
from __future__ import annotations

import json
import logging
import socket
import sqlite3
import threading
import time
import traceback
from typing import Callable

from . import db

log = logging.getLogger("nexagent.jobs")
HANDLERS: dict[str, Callable[[dict], None]] = {}
LEASE_S = 600
_current = threading.local()


def heartbeat() -> None:
    """Long-running handlers call this to keep their lease alive."""
    jid = getattr(_current, "job_id", None)
    if jid:
        with db.tx() as conn:
            conn.execute("UPDATE jobs SET lease_until=?, updated_at=? WHERE id=?", (db.now() + LEASE_S, db.now(), jid))


class PermanentError(Exception):
    """Do not retry (bad input, policy refusal)."""


class Defer(Exception):
    """Try again later without using up an attempt (e.g. training GPU busy)."""

    def __init__(self, seconds: float = 30):
        super().__init__(f"deferred {seconds}s")
        self.seconds = seconds


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def enqueue(conn: sqlite3.Connection, kind: str, payload: dict, *, queue: str = "default",
            idempotency_key: str | None = None, max_attempts: int = 3) -> str:
    if idempotency_key:
        row = conn.execute("SELECT id FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if row:
            return row[0]
    jid = db.new_id()
    t = db.now()
    conn.execute("""INSERT INTO jobs(id,kind,payload,queue,status,max_attempts,idempotency_key,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)""", (jid, kind, json.dumps(payload), queue, "queued", max_attempts,
                                        idempotency_key, t, t))
    return jid


def _lease(queue: str, worker: str) -> dict | None:
    t = db.now()
    with db.tx() as conn:
        row = db.one(conn, """SELECT * FROM jobs WHERE queue=? AND run_after<=? AND
            (status='queued' OR (status='leased' AND lease_until<?)) ORDER BY created_at LIMIT 1""", (queue, t, t))
        if not row:
            return None
        if row["status"] == "leased" and row["attempts"] >= row["max_attempts"]:
            msg = "Lease expired after the last allowed attempt (worker crash or restart)"
            conn.execute("UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?", (msg, t, row["id"]))
            expired = row
        else:
            conn.execute("""UPDATE jobs SET status='leased', attempts=attempts+1, lease_until=?, worker=?, updated_at=?
                WHERE id=?""", (t + LEASE_S, worker, t, row["id"]))
            row["attempts"] += 1
            return row
    hook = HANDLERS.get(expired["kind"] + ":failed")
    if hook:
        try:
            hook(json.loads(expired["payload"]) | {"error": "The server restarted while this was running."})
        except Exception:  # noqa: BLE001
            log.error("failure hook crashed: %s", traceback.format_exc())
    return None


def run_one(queue: str = "default", worker: str = "inline") -> bool:
    job = _lease(queue, worker)
    if not job:
        return False
    fn = HANDLERS.get(job["kind"])
    _current.job_id = job["id"]
    try:
        if fn is None:
            raise PermanentError(f"No handler for job kind {job['kind']}")
        fn(json.loads(job["payload"]))
        with db.tx() as conn:
            conn.execute("UPDATE jobs SET status='done', updated_at=?, error=NULL WHERE id=?", (db.now(), job["id"]))
    except Defer as d:
        with db.tx() as conn:
            conn.execute("UPDATE jobs SET status='queued', attempts=attempts-1, run_after=?, updated_at=? WHERE id=?",
                         (db.now() + d.seconds, db.now(), job["id"]))
    except Exception as exc:  # noqa: BLE001 - recorded, bounded retry
        permanent = isinstance(exc, PermanentError) or job["attempts"] >= job["max_attempts"]
        log.warning("job %s (%s) failed: %s", job["id"], job["kind"], exc)
        with db.tx() as conn:
            conn.execute("UPDATE jobs SET status=?, error=?, run_after=?, updated_at=? WHERE id=?",
                         ("failed" if permanent else "queued", f"{type(exc).__name__}: {exc}"[:1000],
                          db.now() + 5 * job["attempts"], db.now(), job["id"]))
            on_fail = HANDLERS.get(job["kind"] + ":failed")
        if permanent and on_fail:
            try:
                on_fail(json.loads(job["payload"]) | {"error": str(exc)[:500]})
            except Exception:  # noqa: BLE001
                log.error("failure hook for %s crashed: %s", job["kind"], traceback.format_exc())
    finally:
        _current.job_id = None
    return True


class WorkerPool:
    def __init__(self, threads: int = 2):
        self.stop = threading.Event()
        self.threads = [threading.Thread(target=self._loop, args=("default", i), daemon=True) for i in range(threads)]
        self.threads.append(threading.Thread(target=self._loop, args=("training", 0), daemon=True))

    def _loop(self, queue: str, index: int):
        name = f"{socket.gethostname()}:{queue}:{index}"
        while not self.stop.is_set():
            try:
                if not run_one(queue, name):
                    self.stop.wait(0.5)
            except Exception:  # noqa: BLE001
                log.error("worker loop error: %s", traceback.format_exc())
                time.sleep(2)

    def start(self):
        for t in self.threads:
            t.start()

    def shutdown(self):
        self.stop.set()
