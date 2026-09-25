"""Deterministic sequential DAG executor for saved workflow versions.

Semantics
* Nodes run in topological order (ties broken by node id) — the same saved graph
  always runs the same way.
* A node runs when every required input has an *active* value. If a required
  input only comes from skipped branches, the node is skipped.
* Condition nodes emit their input on the 'true' or 'false' port; the other port
  is inactive, so nodes fed only by it are skipped.
* Merge: when several edges feed the same input port, the first active edge in
  saved order wins.
* Only the first Output node that becomes active releases a result.
* Cancellation is checked between nodes and immediately after every model call;
  a late model response after cancellation is discarded.
* Users only ever receive progress events and the final, reviewed result. Draft
  text is never streamed.
"""
from __future__ import annotations

import json
import time
import traceback

from . import db, jobs
from .config import settings
from .graph import CATALOG, topo_order
from .llm import Gateway, GatewayBlocked, ModelUnavailable, query_for_embedding, resolve_model
from .policy import load_actor, max_class, readable_kbs, LEVEL
from .retrieval import retrieve, scope_for_question, still_accessible
from .review import Verdict, evidence_block, review_draft, review_extracts
from .security import api_key_active
from .validators import CITE_RE, is_not_found

SKIP = object()


class Cancelled(Exception):
    pass


class RunFailed(Exception):
    pass


# ------------------------------------------------------------------ events
def emit(run_id: str, type_: str, data: dict) -> None:
    with db.tx() as conn:
        seq = conn.execute("SELECT coalesce(max(seq),0)+1 FROM run_events WHERE run_id=?", (run_id,)).fetchone()[0]
        conn.execute("INSERT INTO run_events(run_id,seq,type,data,created_at) VALUES(?,?,?,?,?)",
                     (run_id, seq, type_, json.dumps(data, ensure_ascii=False), db.now()))


def _cancelled(run_id: str) -> bool:
    with db.read() as conn:
        return bool(conn.execute("SELECT cancel_requested FROM runs WHERE id=?", (run_id,)).fetchone()[0])


def _node_status(run_id, node, status, *, started=None, summary="", detail=None, error=None):
    t = db.now()
    with db.tx() as conn:
        conn.execute("""INSERT INTO run_nodes(run_id,node_id,node_type,status,started_at,finished_at,duration_ms,summary,detail,error)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,node_id) DO UPDATE SET status=excluded.status,
            started_at=coalesce(run_nodes.started_at, excluded.started_at), finished_at=excluded.finished_at,
            duration_ms=excluded.duration_ms, summary=excluded.summary, detail=excluded.detail, error=excluded.error""",
                     (run_id, node["id"], node["type"], status, started,
                      t if status not in ("running",) else None,
                      int((t - started) * 1000) if started and status != "running" else None,
                      summary, json.dumps(detail, ensure_ascii=False) if detail is not None else None, error))
    emit(run_id, "node", {"node_id": node["id"], "node_type": node["type"], "status": status,
                          "summary": summary, "error": error,
                          "duration_ms": int((t - started) * 1000) if started and status != "running" else None})


# ------------------------------------------------------------------ identity
class Identity:
    """Who the run acts for. Grants are re-read from the database on every check."""

    def __init__(self, run: dict):
        self.run = run

    def kbs(self) -> set[str]:
        with db.read() as conn:
            if self.run["api_key_id"]:
                if not api_key_active(conn, self.run["api_key_id"]):
                    return set()
                key = db.one(conn, "SELECT max_classification FROM api_keys WHERE id=?", (self.run["api_key_id"],))
                rows = conn.execute("""SELECT b.id, b.classification FROM api_key_kbs k JOIN knowledge_bases b ON b.id=k.kb_id
                    WHERE k.key_id=?""", (self.run["api_key_id"],)).fetchall()
                return {r[0] for r in rows if LEVEL[r[1]] <= LEVEL[key["max_classification"]]}
            actor = load_actor(conn, self.run["user_id"]) if self.run["user_id"] else None
            return readable_kbs(conn, actor.id, self.run["project_id"]) if actor else set()


# ------------------------------------------------------------------ main
@jobs.handler("run.execute")
def execute_run(payload: dict) -> None:
    run_id = payload["run_id"]
    with db.tx() as conn:
        run = db.one(conn, "SELECT * FROM runs WHERE id=?", (run_id,))
        if not run or run["status"] not in ("queued", "running"):
            return                                      # idempotent: already finished
        version = db.one(conn, "SELECT * FROM workflow_versions WHERE id=?", (run["workflow_version_id"],))
        conn.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
    emit(run_id, "status", {"status": "running", "message": "Started"})
    graph = json.loads(version["graph_json"])
    try:
        Executor(run, graph, owner=version["created_by"]).execute()
    except Cancelled:
        _finish(run_id, "cancelled", None, error="Cancelled by user")
    except (ModelUnavailable, GatewayBlocked, RunFailed) as exc:
        _finish(run_id, "failed", None, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        _finish(run_id, "failed", None, error=f"Internal error ({type(exc).__name__}). See server log.")
        raise jobs.PermanentError(traceback.format_exc()[-800:]) from exc


@jobs.handler("run.execute:failed")
def run_failed(payload: dict) -> None:
    with db.read() as conn:
        row = conn.execute("SELECT status FROM runs WHERE id=?", (payload["run_id"],)).fetchone()
    if row and row[0] in ("queued", "running"):
        _finish(payload["run_id"], "failed", None, error=payload.get("error", "The question could not be completed") +
                " Please ask again.")


def _finish(run_id: str, status: str, answer: dict | None, *, error: str | None = None, classification: str | None = None):
    with db.tx() as conn:
        conn.execute("""UPDATE runs SET status=?, answer_json=?, error=?, finished_at=?,
            classification=coalesce(?, classification) WHERE id=?""",
                     (status, json.dumps(answer, ensure_ascii=False) if answer is not None else None, error,
                      db.now(), classification, run_id))
    emit(run_id, "final", {"status": status, "answer": answer, "error": error})


class Executor:
    def __init__(self, run: dict, graph: dict, owner: str | None = None):
        self.owner = owner              # workflow author: their personal connections / MCP servers are used
        self.run = run
        self.graph = graph
        for n in graph["nodes"]:          # saved graphs may omit settings that have defaults
            n["data"] = {**CATALOG.get(n["type"], {}).get("defaults", {}), **(n.get("data") or {})}
        self.nodes = {n["id"]: n for n in graph["nodes"]}
        self.edges = graph["edges"]
        self.values: dict[tuple[str, str], object] = {}
        self.identity = Identity(run)
        self.classification = run["classification"]
        self.pinned: dict[str, str] = {}
        self.released = False

    # --- helpers
    def check_cancel(self):
        if _cancelled(self.run["id"]):
            raise Cancelled()

    def inputs_for(self, node: dict) -> dict | None:
        spec = CATALOG[node["type"]]
        got = {}
        for port in spec["inputs"]:
            val = SKIP
            for e in self.edges:        # saved order = merge priority
                if e["target"] == node["id"] and e["targetHandle"] == port:
                    v = self.values.get((e["source"], e["sourceHandle"]), SKIP)
                    if v is not SKIP:
                        val = v
                        break
            if val is SKIP and port not in spec.get("optional_inputs", []):
                return None
            got[port] = None if val is SKIP else val
        return got

    def model_for(self, spec: dict, kind: str = "generation_model") -> str:
        with db.read() as conn:
            name = spec.get("model") or db.get_setting(conn, kind, "")
            if not name:
                raise RunFailed("No model selected and no default model is set by the administrator")
            return resolve_model(conn, name)

    def pin_models(self):
        with db.read() as conn:
            for n in self.graph["nodes"]:
                if n["type"] in ("llm", "review", "agent", "team"):
                    d = n.get("data") or {}
                    default = db.get_setting(conn, "review_model" if n["type"] == "review" else "generation_model", "")
                    name = d.get("model") or default
                    if not name:
                        raise RunFailed(f"No model selected for {n['type']} node '{n['id']}' and no default is set")
                    self.pinned[n["id"]] = resolve_model(conn, name)
            self.embedding_model = db.get_setting(conn, "embedding_model", "")
        with db.tx() as conn:
            conn.execute("UPDATE runs SET pinned_json=? WHERE id=?", (json.dumps(self.pinned), self.run["id"]))

    def query_vectors(self, question: str, kb_ids: set[str]) -> dict[str, list[float]]:
        """Embed the question once per distinct embedding setup of the KBs' active indexes."""
        if not kb_ids:
            return {}
        with db.read() as conn:
            gens = db.all_rows(conn, f"""SELECT b.id AS kb_id, b.classification, g.embedding_model, g.embedding_connection,
                g.embedding_owner FROM knowledge_bases b JOIN index_generations g ON g.id=b.active_generation
                WHERE b.id IN ({','.join('?' * len(kb_ids))})""", list(kb_ids))
        groups: dict[tuple, list[dict]] = {}
        for g in gens:
            if g["embedding_model"]:
                groups.setdefault((g["embedding_connection"] or "local-ollama", g["embedding_owner"], g["embedding_model"]),
                                  []).append(g)
        out: dict[str, list[float]] = {}
        self.vector_note = ""
        for (conn_name, owner, model), items in groups.items():
            cls = max_class(self.classification, *(i["classification"] for i in items))
            try:
                vec = Gateway(conn_name, owner=owner).embed([query_for_embedding(model, question)], model, cls)[0]
                out.update({i["kb_id"]: vec for i in items})
            except (ModelUnavailable, GatewayBlocked) as exc:
                self.vector_note = f" Vector search unavailable ({exc}); keyword search only."
        return out

    # --- main loop
    def finish_after_node(self, *args, **kw):
        self.pending_finish = (args, kw)

    def execute(self):
        self.pending_finish = None
        self.pin_models()
        order = topo_order(self.graph["nodes"], self.edges)
        for nid in order:
            self.check_cancel()
            node = self.nodes[nid]
            ins = self.inputs_for(node)
            spec = CATALOG[node["type"]]
            if ins is None or (node["type"] in ("output", "final") and self.released):
                for port in spec["outputs"]:
                    self.values[(nid, port)] = SKIP
                _node_status(self.run["id"], node, "skipped", summary="Not on the active branch" if ins is None
                             else "Another output already released the result")
                continue
            started = db.now()
            _node_status(self.run["id"], node, "running", started=started)
            try:
                outputs, summary, detail = getattr(self, "node_" + node["type"])(node, ins)
            except Cancelled:
                _node_status(self.run["id"], node, "cancelled", started=started, summary="Cancelled")
                raise
            except (ModelUnavailable, GatewayBlocked, RunFailed) as exc:
                _node_status(self.run["id"], node, "failed", started=started, error=str(exc))
                raise
            self.values.update({(nid, p): v for p, v in outputs.items()})
            _node_status(self.run["id"], node, "completed", started=started, summary=summary, detail=detail)
            if self.pending_finish:     # the run's final event goes out after the releasing node's own status
                args, kw = self.pending_finish
                self.pending_finish = None
                _finish(self.run["id"], *args, **kw)
        if not self.released:
            _finish(self.run["id"], "failed", None, error="No Output node was reached")

    # --- node implementations: return (outputs, safe_summary, builder_detail)
    def node_input(self, node, ins):
        q = self.run["question"]
        return {"question": q}, f"Question received ({len(q)} characters)", {"question": q}

    def node_retrieval(self, node, ins):
        d = node.get("data") or {}
        allowed = set(d.get("kb_ids") or []) & self.identity.kbs()
        scope_mode = d.get("scope", "auto")
        if scope_mode == "auto":
            sections, reason = scope_for_question(ins["question"])
        elif scope_mode == "fixed":
            sections, reason = (d.get("section_types") or None), "fixed section types"
        else:
            sections, reason = None, "all section types"
        vectors = self.query_vectors(ins["question"], allowed) if allowed else {}
        note = getattr(self, "vector_note", "")
        self.check_cancel()
        with db.read() as conn:
            items = retrieve(conn, ins["question"], allowed, top_k=int(d.get("top_k", 6)),
                             section_types=sections, vectors_by_kb=vectors, graph=d.get("graph", "off"))
        evidence = [i.public() for i in items]
        self.classification = max_class(self.classification, *(e["classification"] for e in evidence))
        self.allowed_sections = sections
        docs = {e["document_id"] for e in evidence}
        summary = (f"{len(evidence)} passage(s) from {len(docs)} document(s); scope: {reason}.{note}"
                   if allowed else "No accessible knowledge base for this user." + note)
        return {"evidence": evidence}, summary, {"scope": sections, "evidence": [
            {k: e[k] for k in ("ref", "filename", "revision_no", "section", "section_type", "location", "score")}
            for e in evidence]}

    def node_prompt(self, node, ins):
        d = node.get("data") or {}
        evidence = ins.get("evidence") or []
        context = evidence_block(evidence) if evidence else "(no sources available)"
        user = (d.get("template") or "{question}").replace("{context}", context).replace("{question}", ins["question"])
        messages = [{"role": "system", "content": d.get("system") or CATALOG["prompt"]["defaults"]["system"]},
                    {"role": "user", "content": user}]
        self.last_prompt = messages
        return {"prompt": messages}, f"Prompt built with {len(evidence)} source(s)", {"system": messages[0]["content"]}

    def node_llm(self, node, ins):
        d = node.get("data") or {}
        gw = Gateway(d.get("connection") or "local-ollama", owner=self.owner)
        res = gw.chat(ins["prompt"], self.pinned[node["id"]], self.classification,
                      temperature=float(d.get("temperature", 0.1)), max_tokens=int(d.get("max_tokens", 800)))
        self.check_cancel()                                    # late responses after cancel are discarded
        self.generator = (gw, self.pinned[node["id"]], d, ins["prompt"])
        return ({"draft": res.text}, f"Draft generated by {res.model} in {res.latency_ms} ms (held for review)",
                {"draft": res.text, "model": res.model, "latency_ms": res.latency_ms})

    def node_review(self, node, ins):
        d = node.get("data") or {}
        evidence = ins["evidence"] or []
        allowed_sections = getattr(self, "allowed_sections", None)
        reviewer = None
        if d.get("model_review", True):
            gw = Gateway(d.get("connection") or "local-ollama", owner=self.owner)
            model = self.pinned[node["id"]]

            def reviewer(messages):
                text = gw.chat(messages, model, self.classification, temperature=0.0, max_tokens=700, json_mode=True).text
                self.check_cancel()
                return text
        draft = ins["draft"] or ""
        result = review_draft(ins["question"], draft, evidence, allowed_sections=allowed_sections, model_reviewer=reviewer)
        repaired = False
        if result.verdict == Verdict.REVISE and d.get("allow_repair", True) and getattr(self, "generator", None):
            gw, model, gd, prompt = self.generator
            findings = "\n".join(f"- {i.code}: {i.explanation} (claim: {i.claim[:160]})" for i in result.issues if i.blocking)
            repair_msgs = list(prompt) + [
                {"role": "assistant", "content": draft},
                {"role": "user", "content": "A reviewer found these problems:\n" + findings +
                 "\nRewrite the answer using only the numbered sources, citing each plant-specific statement. "
                 "Keep tags, values, units, conditions and limits exactly as in the sources. If the sources do not "
                 "contain the answer, reply exactly: \"The answer was not found in the accessible sources.\""}]
            draft = gw.chat(repair_msgs, model, self.classification, temperature=0.0,
                            max_tokens=int(gd.get("max_tokens", 800))).text
            self.check_cancel()
            repaired = True
            result = review_draft(ins["question"], draft, evidence, allowed_sections=allowed_sections,
                                  model_reviewer=reviewer)
        if result.verdict == Verdict.REVISE:          # at most one repair: still failing → engineer
            result.verdict = Verdict.HUMAN
        reviewed = {"verdict": str(result.verdict), "answer": draft, "review": result.public(), "evidence": evidence,
                    "question": ins["question"], "repaired": repaired, "fallback": d.get("fallback", "extract"),
                    "allowed_sections": allowed_sections}
        codes = sorted({i["code"] for i in result.public()["issues"]})
        summary = f"Verdict: {result.verdict}{' after one repair' if repaired else ''}" + \
                  (f" — issues: {', '.join(codes)}" if codes else "")
        return {"reviewed": reviewed}, summary, {"review": result.public(), "draft": draft}

    def node_condition(self, node, ins):
        d = node.get("data") or {}
        v = ins["value"]
        rule = d.get("rule")
        if rule == "verdict_is":
            ok = isinstance(v, dict) and v.get("verdict") == d.get("argument")
        elif rule == "draft_contains":
            ok = isinstance(v, str) and str(d.get("argument", "")).lower() in v.lower()
        elif rule == "has_evidence":
            ok = bool(v)
        else:
            ok = isinstance(v, str) and str(d.get("argument", "")).lower() in v.lower()
        return ({"true": v if ok else SKIP, "false": SKIP if ok else v},
                f"Condition '{rule}' is {'true' if ok else 'false'}", {"result": ok})

    def node_output(self, node, ins):
        r = ins["answer"]
        self.released = True
        kbs = self.identity.kbs()
        cited_refs = sorted({int(x) for m in CITE_RE.finditer(r["answer"]) for x in m.group(1).split(",")})
        by_ref = {e["ref"]: e for e in r["evidence"]}
        cited = [by_ref[i] for i in cited_refs if i in by_ref]
        with db.tx() as conn:
            if self.run["api_key_id"] and not api_key_active(conn, self.run["api_key_id"]):
                raise RunFailed("The API key was revoked while the request was running")
            accessible = still_accessible(conn, [e["chunk_id"] for e in r["evidence"]], kbs)
        if r["verdict"] == Verdict.PASS:
            if any(e["chunk_id"] not in accessible for e in cited):
                raise RunFailed("Access to a cited source was revoked before release; nothing was released")
            status = "not_found" if is_not_found(r["answer"]) else "completed"
            answer = {"answer": r["answer"], "citations": [_citation(e) for e in cited],
                      "review": {"verdict": "pass", "reviewer": r["review"]["reviewer"],
                                 "repaired": r["repaired"]},
                      "notice": "" if status == "completed" else "No accessible source contains this answer."}
            self.finish_after_node(status, answer, classification=self.classification)
            return {}, f"Released ({status})", {}
        if not settings().has("review"):
            # No engineer queue on this server: never release the unverified draft; offer quoted extracts instead.
            scoped = [e for e in r["evidence"] if not r["allowed_sections"] or e["section_type"] in r["allowed_sections"]]
            scoped = [e for e in scoped if e["chunk_id"] in accessible and e["section_type"] != "graph_summary"][:2]
            answer = {"answer": "", "citations": [], "review": {"verdict": r["verdict"], "issues": r["review"]["issues"]},
                      "notice": "The generated answer could not be verified against the sources, so it is not shown.",
                      "extracts": [_citation(e) for e in scoped]}
            if scoped:
                answer["notice"] += " These source passages, quoted word for word, may help."
            self.finish_after_node("not_found", answer, classification=self.classification)
            return {}, "Not verified — draft withheld (no review queue on this server)", {}
        # needs human review: create the queue item, optionally release attributed extracts meanwhile
        item_id = db.new_id()
        with db.tx() as conn:
            conn.execute("""INSERT INTO review_items(id,run_id,project_id,question,draft,evidence_json,findings_json,
                classification,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                         (item_id, self.run["id"], self.run["project_id"], r["question"], r["answer"],
                          json.dumps(r["evidence"], ensure_ascii=False), json.dumps(r["review"], ensure_ascii=False),
                          self.classification, "open", db.now()))
            db.audit(conn, self.run["user_id"], "review.queued", "review_item", item_id,
                     {"run_id": self.run["id"], "codes": sorted({i["code"] for i in r["review"]["issues"]})})
        interim = {"answer": "", "citations": [], "review": {"verdict": "needs_human_review"},
                   "notice": "An engineer is reviewing this answer. It will appear in your history once approved.",
                   "review_item_id": item_id}
        if r["fallback"] == "extract" and r["evidence"]:
            scoped = [e for e in r["evidence"] if not r["allowed_sections"] or e["section_type"] in r["allowed_sections"]][:2]
            gate = review_extracts([{"chunk_id": e["chunk_id"], "text": e["text"]} for e in scoped], r["evidence"],
                                   currently_accessible=accessible, reviewer_verdict=Verdict.PASS,
                                   allowed_sections=r["allowed_sections"])
            if gate.verdict == Verdict.PASS and scoped:
                interim["extracts"] = [_citation(e) for e in scoped]
                interim["notice"] += " Meanwhile, here are the most relevant source extracts, quoted word for word."
        elif r["fallback"] == "not_found":
            interim["notice"] = ("The answer could not be verified against the accessible sources. "
                                 "An engineer has been asked to review it.")
        self.finish_after_node("awaiting_review", interim, classification=self.classification)
        return {}, "Sent to engineer review", {"review_item_id": item_id}


def _agent_node(self, node, ins, team: bool):
    from .agents import AgentRuntime
    from .graph import agent_specs
    d = dict(node.get("data") or {})
    d["model"] = self.pinned.get(node["id"]) or d.get("model")
    rt = AgentRuntime(self, ins["question"], ins.get("evidence"))
    specs = agent_specs({**node, "data": d})
    answer = rt.run(specs[0], ins["question"], members=specs[1:] if team else None)
    tools_used = sum(1 for t in rt.trace if t["type"] == "tool")
    label = "Team" if team else f"Agent '{d.get('name', 'agent')}'"
    return ({"answer": answer, "evidence": rt.evidence},
            f"{label} finished after {tools_used} tool call(s); {len(rt.evidence)} source(s) (answer held for release step)",
            {"trace": rt.trace, "draft": answer})


def _final_node(self, node, ins):
    self.released = True
    answer = ins["answer"] or ""
    evidence = ins.get("evidence") or []
    refs = sorted({int(x) for m in CITE_RE.finditer(answer) for x in m.group(1).split(",")})
    by_ref = {e["ref"]: e for e in evidence}
    cited = [by_ref[r] for r in refs if r in by_ref]
    with db.tx() as conn:
        if self.run["api_key_id"] and not api_key_active(conn, self.run["api_key_id"]):
            raise RunFailed("The API key was revoked while the request was running")
        ok = still_accessible(conn, [e["chunk_id"] for e in cited], self.identity.kbs()) if cited else set()
    if any(e["chunk_id"] not in ok for e in cited):
        raise RunFailed("Access to a cited source was revoked before release; nothing was released")
    status = "not_found" if is_not_found(answer) else "completed"
    self.finish_after_node(status, {"answer": answer, "citations": [_citation(e) for e in cited],
                                     "review": {"verdict": "not_reviewed"},
                                     "notice": "Released without Answer Review (this project does not require it)."},
            classification=self.classification)
    return {}, f"Released ({status}, not reviewed)", {}


Executor.node_agent = lambda self, node, ins: _agent_node(self, node, ins, team=False)
Executor.node_team = lambda self, node, ins: _agent_node(self, node, ins, team=True)
Executor.node_final = _final_node


def _citation(e: dict) -> dict:
    return {k: e[k] for k in ("ref", "chunk_id", "document_id", "filename", "revision_no", "section", "section_type",
                              "location", "text")}
