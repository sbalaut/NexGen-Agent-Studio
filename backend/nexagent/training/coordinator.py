"""Supervised training coordinator (runs inside the app; spawns the runner process).

Lifecycle: pending_approval → (preflight) → blocked | queued → running → completed | failed | cancelled
 * The requester cannot approve their own job.
 * Preflight failures mark the job 'blocked' with the exact missing prerequisites.
 * Only train + validation rows are written into the job folder; held-out rows are not.
 * One training job at a time on the 'training' queue; production inference is not preempted
   automatically — pin training to a GPU with NEXAGENT_TRAIN_GPU.
 * Metrics shown are exactly those the runner logs; nothing is simulated.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from .. import db, jobs
from ..config import settings
from ..datasets import heldout_set, training_export
from ..policy import max_class
from ..review import review_draft
from ..retrieval import scope_for_question
from ..validators import TAG_RE, devanagari_share, is_not_found, quantities
from .runner import BOUNDS, RECIPES

RUNNER = Path(__file__).resolve().parent / "runner.py"


def train_python() -> str:
    return settings().train_python or sys.executable


def job_dir(job_id: str) -> Path:
    return settings().training_dir / job_id


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("NEXAGENT_")}
    env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                "PYTHONUNBUFFERED": "1", "NO_PROXY": "*"})
    for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        env.pop(proxy, None)                            # no egress by default
    if settings().train_gpu:
        env["CUDA_VISIBLE_DEVICES"] = settings().train_gpu
    return env


def validate_params(recipe: str, params: dict) -> dict:
    if recipe not in RECIPES:
        raise ValueError(f"Recipe '{recipe}' is not allow-listed. Choose one of {sorted(RECIPES)}")
    clean = {}
    for k, v in (params or {}).items():
        if k not in BOUNDS:
            raise ValueError(f"Unknown training parameter '{k}'")
        lo, hi = BOUNDS[k]
        if not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"Parameter {k} must be between {lo} and {hi}")
        clean[k] = v
    return clean


def base_model_path_ok(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    root = settings().models_dir.resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"Base models must be inside the approved folder {root} (NEXAGENT_MODELS_DIR); "
                         "copy the model files there first. Nothing is downloaded automatically.")
    return p


def run_preflight(job: dict, model_path: str, examples: int) -> dict:
    d = job_dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run([train_python(), str(RUNNER), "preflight", "--model", model_path, "--workdir", str(d),
                               "--recipe", job["recipe"], "--examples", str(examples)],
                              capture_output=True, text=True, timeout=300, env=_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "checks": [{"name": "training_python", "ok": False, "blocking": True,
                                          "detail": f"Cannot run the training environment ({train_python()}): {exc}"}]}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"ok": False, "checks": [{"name": "training_python", "ok": False, "blocking": True,
                                          "detail": "Training environment failed to start: "
                                                    + (proc.stderr.strip().splitlines() or ["no output"])[-1][:300]}]}
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------------ jobs
@jobs.handler("training.preflight")
def preflight_job(payload: dict) -> None:
    with db.read() as conn:
        job = db.one(conn, "SELECT * FROM training_jobs WHERE id=?", (payload["job_id"],))
        bm = db.one(conn, "SELECT * FROM base_models WHERE id=?", (job["base_model_id"],))
        try:
            export = training_export(conn, job["dataset_version_id"])
            n = len(export["train"])
            ds_problem = None
        except ValueError as exc:
            n, ds_problem = 0, str(exc)
    result = run_preflight(job, bm["path"], n)
    if ds_problem:
        result["ok"] = False
        result["checks"].append({"name": "dataset_access", "ok": False, "blocking": True, "detail": ds_problem})
    else:
        result["checks"].append({"name": "dataset_access", "ok": True, "blocking": True,
                                 "detail": f"dataset hash verified; {n} train example(s)"})
    with db.tx() as conn:
        status = "queued" if result["ok"] else "blocked"
        conn.execute("UPDATE training_jobs SET status=?, preflight_json=? WHERE id=? AND status='pending_approval'",
                     (status, json.dumps(result), job["id"]))
        if status == "queued":
            jobs.enqueue(conn, "training.start", {"job_id": job["id"]}, queue="training",
                         idempotency_key="train:" + job["id"], max_attempts=3)
        db.audit(conn, None, "training.preflight_" + ("passed" if result["ok"] else "blocked"), "training_job",
                 job["id"], {"failed": [c["name"] for c in result["checks"] if c["blocking"] and not c["ok"]]})


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@jobs.handler("training.start")
def start_job(payload: dict) -> None:
    jid = payload["job_id"]
    with db.tx() as conn:
        job = db.one(conn, "SELECT * FROM training_jobs WHERE id=?", (jid,))
        if job["status"] not in ("queued", "running", "cancelling"):
            return
        other = conn.execute("SELECT 1 FROM training_jobs WHERE status IN ('running','cancelling') AND id<>?",
                             (jid,)).fetchone()
        if other:
            raise jobs.Defer(30)
        if job["status"] == "cancelling":
            conn.execute("UPDATE training_jobs SET status='cancelled', finished_at=? WHERE id=?", (db.now(), jid))
            return
        bm = db.one(conn, "SELECT * FROM base_models WHERE id=?", (job["base_model_id"],))
        export = training_export(conn, job["dataset_version_id"])
    d = job_dir(jid)
    d.mkdir(parents=True, exist_ok=True)
    resumed = job["status"] == "running" and not _pid_alive(job["pid"])
    if job["status"] == "running" and _pid_alive(job["pid"]):
        proc_pid = job["pid"]                      # we restarted but the runner survived: keep monitoring
    else:
        (d / "config.json").write_text(json.dumps({"base_model_path": bm["path"], "recipe": job["recipe"],
                                                   "params": json.loads(job["params_json"]), "seed": job["seed"],
                                                   "dataset_sha256": export["dataset_sha256"]}))
        (d / "train.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in export["train"]))
        (d / "validation.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in export["validation"]))
        for stale in ("CANCEL", "status.json"):
            (d / stale).unlink(missing_ok=True)
        log = open(d / "runner.log", "a")
        proc = subprocess.Popen([train_python(), str(RUNNER), "train", "--job", str(d)], stdout=log, stderr=log,
                                env=_env(), cwd=str(d), start_new_session=True,
                                preexec_fn=(lambda: os.nice(10)) if hasattr(os, "nice") else None)
        proc_pid = proc.pid
        with db.tx() as conn:
            conn.execute("""UPDATE training_jobs SET status='running', pid=?, output_dir=?,
                started_at=coalesce(started_at, ?) WHERE id=?""", (proc_pid, str(d), db.now(), jid))
            db.audit(conn, None, "training.resumed_after_restart" if resumed else "training.started", "training_job",
                     jid, {"pid": proc_pid, "dataset_sha256": export["dataset_sha256"]})
    _monitor(jid, proc_pid, d)


def _ingest_metrics(jid: str, d: Path, offset: int) -> int:
    p = d / "metrics.jsonl"
    if not p.exists():
        return offset
    with p.open() as fh:
        fh.seek(offset)
        lines = fh.readlines()
        offset = fh.tell()
    rows = []
    for line in lines:
        try:
            m = json.loads(line)
            rows.append((jid, m["step"], m.get("epoch"), m.get("loss"), m.get("eval_loss"), m.get("learning_rate"), m["time"]))
        except (json.JSONDecodeError, KeyError):
            continue
    if rows:
        with db.tx() as conn:
            conn.executemany("""INSERT INTO training_metrics(job_id,step,epoch,loss,eval_loss,learning_rate,created_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id,step) DO UPDATE SET
                loss=coalesce(excluded.loss, loss), eval_loss=coalesce(excluded.eval_loss, eval_loss),
                epoch=excluded.epoch, learning_rate=coalesce(excluded.learning_rate, learning_rate)""", rows)
    return offset


def _monitor(jid: str, pid: int, d: Path) -> None:
    offset = 0
    deadline = time.time() + settings().train_max_hours * 3600
    while True:
        offset = _ingest_metrics(jid, d, offset)
        jobs.heartbeat()
        with db.read() as conn:
            status = conn.execute("SELECT status FROM training_jobs WHERE id=?", (jid,)).fetchone()[0]
        if status == "cancelling" or time.time() > deadline:
            (d / "CANCEL").touch()
        alive = _pid_alive(pid)
        if alive:
            try:                                         # reap if it is our child
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    alive = False
            except ChildProcessError:
                pass
        if not alive:
            break
        time.sleep(2)
    offset = _ingest_metrics(jid, d, offset)
    _finalize(jid, d, timed_out=time.time() > deadline)


def _finalize(jid: str, d: Path, timed_out: bool) -> None:
    st = json.loads((d / "status.json").read_text()) if (d / "status.json").exists() else \
        {"state": "failed", "error": "The runner exited without writing a status file. See runner.log."}
    state = st.get("state")
    with db.tx() as conn:
        job = db.one(conn, "SELECT * FROM training_jobs WHERE id=?", (jid,))
        ds = db.one(conn, "SELECT classification, sha256 FROM dataset_versions WHERE id=?", (job["dataset_version_id"],))
        if state == "completed":
            aid = db.new_id()
            conn.execute("""INSERT INTO model_artifacts(id,training_job_id,kind,path,files_json,classification,status,created_at)
                VALUES(?,?,?,?,?,?,?,?)""", (aid, jid, "lora_adapter", str(d / "adapter"),
                                             json.dumps(st.get("adapter_files", {})), ds["classification"],
                                             "registered", db.now()))
            conn.execute("""UPDATE training_jobs SET status='completed', finished_at=?, software_json=?, hardware_json=?,
                error=NULL WHERE id=?""", (db.now(), json.dumps(st.get("software")), json.dumps(st.get("hardware")), jid))
            db.audit(conn, None, "training.completed", "training_job", jid,
                     {"artifact": aid, "train_loss": st.get("train_loss"), "dataset_sha256": ds["sha256"]})
        elif state == "cancelled" or job["status"] == "cancelling":
            msg = "Stopped: time limit NEXAGENT_TRAIN_MAX_HOURS reached" if timed_out else "Cancelled by operator"
            conn.execute("UPDATE training_jobs SET status='cancelled', finished_at=?, error=? WHERE id=?",
                         (db.now(), msg + ". Checkpoints are kept in the job folder.", jid))
            db.audit(conn, None, "training.cancelled", "training_job", jid, {})
        else:
            conn.execute("UPDATE training_jobs SET status='failed', finished_at=?, error=? WHERE id=?",
                         (db.now(), (st.get("error") or "Runner failed")[:1500], jid))
            db.audit(conn, None, "training.failed", "training_job", jid, {"code": st.get("error_code")})


def request_cancel(conn, jid: str, actor_id: str) -> str:
    job = db.one(conn, "SELECT * FROM training_jobs WHERE id=?", (jid,))
    if job["status"] in ("pending_approval", "blocked", "queued"):
        conn.execute("UPDATE training_jobs SET status='cancelled', finished_at=? WHERE id=?", (db.now(), jid))
        new = "cancelled"
    elif job["status"] == "running":
        conn.execute("UPDATE training_jobs SET status='cancelling' WHERE id=?", (jid,))
        new = "cancelling"
    else:
        raise ValueError(f"A {job['status']} job cannot be cancelled")
    db.audit(conn, actor_id, "training.cancel_requested", "training_job", jid, {"new_status": new})
    return new


# ------------------------------------------------------------------ evaluation
def token_f1(a: str, b: str) -> float:
    ta, tb = re.findall(r"\w+", a.lower()), re.findall(r"\w+", b.lower())
    if not ta or not tb:
        return float(ta == tb)
    common = sum(min(ta.count(t), tb.count(t)) for t in set(ta))
    if common == 0:
        return 0.0
    p, r = common / len(ta), common / len(tb)
    return 2 * p * r / (p + r)


def score(rows: list[dict], outputs: list[dict]) -> dict:
    """Deterministic scores (no model judge). Documented in the UI as 'deterministic'."""
    by_id = {o["id"]: o for o in outputs}
    n_ans = n_unans = useful = false_ans = abstain = passes = preserved_ok = preserved_n = leak = 0
    f1s, lat, hi_f1 = [], [], []
    for r in rows:
        o = by_id.get(r["id"], {"output": "", "latency_ms": 0})
        out, ref = o["output"], r["answer"]
        answerable = not is_not_found(ref)
        f1 = token_f1(out, ref)
        f1s.append(f1)
        lat.append(o.get("latency_ms", 0))
        sections, _ = scope_for_question(r["question"])
        ev = [dict(e, classification="Internal", equipment_tags=sorted(set(TAG_RE.findall(e["text"]))))
              for e in r["evidence"]]
        rv = review_draft(r["question"], out, ev, allowed_sections=sections, model_reviewer=None)
        ok = rv.verdict == "pass"
        passes += ok
        leak += any(i.code == "section_out_of_scope" for i in rv.issues)
        if devanagari_share(r["question"]) > 0.3:
            hi_f1.append(f1)
        if answerable:
            n_ans += 1
            if is_not_found(out):
                abstain += 1
            elif ok and f1 >= 0.5:
                useful += 1
            ref_items = {(q.a, q.b, q.unit) for q in quantities(ref)} | set(TAG_RE.findall(ref))
            out_items = {(q.a, q.b, q.unit) for q in quantities(out)} | set(TAG_RE.findall(out))
            preserved_n += len(ref_items)
            preserved_ok += len(ref_items & out_items)
        else:
            n_unans += 1
            if not is_not_found(out):
                false_ans += 1
    n = len(rows) or 1
    return {
        "examples": len(rows),
        "raw_token_f1_mean": round(sum(f1s) / n, 4),
        "passes_answer_review_checks": round(passes / n, 4),
        "useful_answer_rate_answerable": round(useful / n_ans, 4) if n_ans else None,
        "abstention_rate_answerable": round(abstain / n_ans, 4) if n_ans else None,
        "false_answer_rate_unanswerable": round(false_ans / n_unans, 4) if n_unans else None,
        "numeric_tag_preservation": round(preserved_ok / preserved_n, 4) if preserved_n else None,
        "scope_leakage_rate": round(leak / n, 4),
        "hindi_token_f1_mean": round(sum(hi_f1) / len(hi_f1), 4) if hi_f1 else None,
        "latency_ms_mean": int(sum(lat) / n),
    }


@jobs.handler("evaluation.run")
def evaluation_job(payload: dict) -> None:
    eid = payload["evaluation_id"]
    with db.tx() as conn:
        ev = db.one(conn, "SELECT * FROM evaluations WHERE id=?", (eid,))
        art = db.one(conn, "SELECT * FROM model_artifacts WHERE id=?", (ev["artifact_id"],))
        rows = heldout_set(conn, ev["dataset_version_id"])
        conn.execute("UPDATE evaluations SET status='running' WHERE id=?", (eid,))
    tj = Path(art["path"]).parent
    edir = settings().training_dir / ("eval-" + eid)             # separate folder from the training job inputs
    edir.mkdir(parents=True, exist_ok=True)
    (edir / "heldout.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
    proc = subprocess.run([train_python(), str(RUNNER), "evaluate", "--job", str(tj), "--heldout",
                           str(edir / "heldout.jsonl"), "--out", str(edir / "generations.json")],
                          capture_output=True, text=True, env=_env(), timeout=6 * 3600)
    out_path = edir / "generations.json"
    gen = json.loads(out_path.read_text()) if out_path.exists() else {"state": "failed", "error": proc.stderr[-500:]}
    with db.tx() as conn:
        if gen.get("state") != "completed":
            conn.execute("UPDATE evaluations SET status='failed', error=?, finished_at=? WHERE id=?",
                         (gen.get("error", "evaluation failed")[:1000], db.now(), eid))
            return
        results = {"method": "deterministic (token-F1, validators, numeric/tag preservation); no model judge",
                   "comparison": "base model weights vs base+candidate adapter, identical prompts, frozen held-out set, greedy decoding",
                   "settings": gen["settings"], "base": score(rows, gen["base"]), "candidate": score(rows, gen["candidate"]),
                   "hardware": gen.get("hardware"), "software": gen.get("software"),
                   "samples": [{"id": r["id"], "question": r["question"][:200],
                                "base": b["output"][:400], "candidate": c["output"][:400]}
                               for r, b, c in list(zip(rows, gen["base"], gen["candidate"]))[:10]]}
        conn.execute("UPDATE evaluations SET status='completed', results_json=?, finished_at=? WHERE id=?",
                     (json.dumps(results), db.now(), eid))
        db.audit(conn, ev["requested_by"], "evaluation.completed", "evaluation", eid, {})


@jobs.handler("evaluation.run:failed")
def evaluation_failed(payload: dict) -> None:
    with db.tx() as conn:
        conn.execute("UPDATE evaluations SET status='failed', error=?, finished_at=? WHERE id=?",
                     (payload.get("error", "failed"), db.now(), payload["evaluation_id"]))


# ------------------------------------------------------------------ deployment
@jobs.handler("deployment.export")
def export_deployment(payload: dict) -> None:
    """Merge adapter → import into Ollama → verify it answers → switch alias atomically."""
    did = payload["deployment_id"]
    with db.read() as conn:
        dep = db.one(conn, "SELECT * FROM deployments WHERE id=?", (did,))
        art = db.one(conn, "SELECT * FROM model_artifacts WHERE id=?", (dep["artifact_id"],))
    tj = Path(art["path"]).parent
    merged = tj / ("merged-" + did)

    def fail(msg: str):
        with db.tx() as conn:
            conn.execute("UPDATE deployments SET status='export_failed', error=?, updated_at=? WHERE id=?",
                         (msg[:1500], db.now(), did))
            db.audit(conn, None, "deployment.export_failed", "deployment", did, {"error": msg[:300]})

    subprocess.run([train_python(), str(RUNNER), "merge", "--job", str(tj), "--out", str(merged)],
                   capture_output=True, text=True, env=_env(), timeout=6 * 3600)
    info = json.loads((merged / "nexagent-merge.json").read_text()) if (merged / "nexagent-merge.json").exists() else {}
    if info.get("state") != "completed":
        return fail("Merging the adapter failed: " + info.get("error", "no result"))
    name = f"nexagent-{dep['alias']}:{did[:8]}"
    (merged / "Modelfile").write_text(f"FROM {merged}\n")
    try:
        p = subprocess.run([settings().ollama_cli, "create", name, "-f", str(merged / "Modelfile")],
                           capture_output=True, text=True, timeout=3 * 3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return fail(f"Could not run '{settings().ollama_cli} create': {exc}. Import the merged model manually "
                    f"from {merged} and retry.")
    if p.returncode != 0:
        return fail("Ollama could not import the merged model (architecture may be unsupported for direct "
                    f"safetensors import): {p.stderr.strip()[-600:]}")
    from ..llm import Gateway
    try:   # serving compatibility check on the deployed artifact itself
        Gateway().chat([{"role": "user", "content": "Reply with the word OK."}], name, "Public", max_tokens=8)
    except Exception as exc:  # noqa: BLE001
        return fail(f"Imported model did not answer through Ollama: {exc}")
    with db.tx() as conn:
        cur = db.one(conn, "SELECT * FROM model_aliases WHERE alias=?", (dep["alias"],))
        if cur:
            conn.execute("UPDATE deployments SET status='superseded', updated_at=? WHERE id=?", (db.now(), cur["deployment_id"]))
        conn.execute("""INSERT INTO model_aliases(alias,deployment_id,previous_deployment_id,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(alias) DO UPDATE SET previous_deployment_id=model_aliases.deployment_id,
            deployment_id=excluded.deployment_id, updated_at=excluded.updated_at""",
                     (dep["alias"], did, cur["deployment_id"] if cur else None, db.now()))
        conn.execute("UPDATE deployments SET status='active', ollama_model=?, updated_at=?, error=NULL WHERE id=?",
                     (name, db.now(), did))
        db.audit(conn, dep["approved_by"], "deployment.activated", "deployment", did,
                 {"alias": dep["alias"], "ollama_model": name,
                  "note": "Merged artifact differs from the evaluated adapter format; re-run the frozen evaluation "
                          "workflow against 'promoted:" + dep["alias"] + "' before wide use."})


def rollback(conn, alias: str, actor_id: str) -> dict:
    cur = db.one(conn, "SELECT * FROM model_aliases WHERE alias=?", (alias,))
    if not cur or not cur["previous_deployment_id"]:
        raise ValueError("There is no previous deployment to roll back to")
    prev = db.one(conn, "SELECT * FROM deployments WHERE id=?", (cur["previous_deployment_id"],))
    conn.execute("UPDATE deployments SET status='rolled_back', updated_at=? WHERE id=?", (db.now(), cur["deployment_id"]))
    conn.execute("UPDATE deployments SET status='active', updated_at=? WHERE id=?", (db.now(), prev["id"]))
    conn.execute("UPDATE model_aliases SET deployment_id=?, previous_deployment_id=NULL, updated_at=? WHERE alias=?",
                 (prev["id"], db.now(), alias))
    db.audit(conn, actor_id, "deployment.rolled_back", "model_alias", None,
             {"alias": alias, "from": cur["deployment_id"], "to": prev["id"]})
    return {"alias": alias, "active_deployment": prev["id"]}


__all__ = ["validate_params", "base_model_path_ok", "request_cancel", "rollback", "score", "max_class"]
