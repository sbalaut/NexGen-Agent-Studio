"""Workflow graph schema and server-side validation.

A graph is {"nodes":[{"id","type","position":{x,y},"data":{...settings}}],
            "edges":[{"id","source","sourceHandle","target","targetHandle"}]}

Typed ports make the mandatory safety rule structural: the Output node only
accepts a `reviewed` answer, and only an Answer Review node produces one, so no
path can deliver an unreviewed model draft to a user.
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict, deque

from .documents import SECTION_TYPES

# type -> {inputs: {port: type}, outputs: {port: type}, required settings, defaults}
CATALOG: dict[str, dict] = {
    "input": {"label": "Input", "inputs": {}, "outputs": {"question": "text"},
              "defaults": {}, "description": "The user's question enters here."},
    "retrieval": {"label": "Knowledge Retrieval", "inputs": {"question": "text"}, "outputs": {"evidence": "evidence"},
                  "defaults": {"kb_ids": [], "top_k": 6, "scope": "auto", "section_types": [], "graph": "off"},
                  "required": ["kb_ids"],
                  "description": "Finds matching passages in the selected knowledge bases (only those the user may read)."},
    "prompt": {"label": "Prompt", "inputs": {"question": "text", "evidence": "evidence"}, "outputs": {"prompt": "prompt"},
               "optional_inputs": ["evidence"],
               "defaults": {"system": "You are a careful refinery assistant. Answer only from the numbered sources. "
                                      "Cite every plant-specific statement with its source number like [1]. Keep "
                                      "equipment tags, numbers, units and conditions exactly as written. If the "
                                      "sources do not contain the answer, reply exactly: \"The answer was not found "
                                      "in the accessible sources.\" Answer in the language of the question. Give each paragraph a short "
                                      "heading line starting with '### '; headings may use your own words, but every fact "
                                      "must come from the sources.",
                            "template": "SOURCES:\n{context}\n\nQUESTION:\n{question}"},
               "required": ["template"],
               "description": "Builds the instructions sent to the model."},
    "llm": {"label": "LLM", "inputs": {"prompt": "prompt"}, "outputs": {"draft": "draft"},
            "defaults": {"connection": "local-ollama", "model": "", "temperature": 0.1, "max_tokens": 800},
            "description": "Generates a draft answer with a local model."},
    "review": {"label": "Answer Review", "inputs": {"draft": "draft", "evidence": "evidence", "question": "text"},
               "outputs": {"reviewed": "reviewed"},
               "defaults": {"model_review": True, "connection": "local-ollama", "model": "", "allow_repair": True,
                            "fallback": "extract"},
               "description": "Checks the draft against the sources; failing answers go to an engineer."},
    "condition": {"label": "Condition", "inputs": {"value": "any"}, "outputs": {"true": "same", "false": "same"},
                  "defaults": {"rule": "verdict_is", "argument": "pass"},
                  "description": "Sends the value down the 'true' or 'false' branch."},
    "output": {"label": "Output", "inputs": {"answer": "reviewed"}, "outputs": {},
               "defaults": {}, "description": "Releases the reviewed answer (or sends it to engineer review)."},
    "agent": {"label": "Agent", "inputs": {"question": "text", "evidence": "evidence"},
              "outputs": {"answer": "draft", "evidence": "evidence"}, "optional_inputs": ["evidence"],
              "defaults": {"name": "Assistant", "connection": "local-ollama", "model": "", "temperature": 0.2,
                           "max_tokens": 1200, "max_steps": 6, "kb_ids": [], "mcp_tools": [],
                           "instructions": "You are a helpful assistant. Use the tools when they help. When you use "
                                           "the knowledge search, cite passages by their number like [1]. If you cannot "
                                           "find the answer, say so."},
              "description": "An AI agent that decides which tools to call (knowledge search, MCP tools) before answering."},
    "team": {"label": "Agent Team", "inputs": {"question": "text", "evidence": "evidence"},
             "outputs": {"answer": "draft", "evidence": "evidence"}, "optional_inputs": ["evidence"],
             "defaults": {"connection": "local-ollama", "model": "", "temperature": 0.2, "max_tokens": 1500,
                          "max_steps": 8,
                          "instructions": "You lead a team of specialist agents. Break the request into parts, delegate "
                                          "each part to the best specialist, then combine their findings into one clear "
                                          "answer. Keep source numbers like [1] that specialists cite.",
                          "members": [{"name": "researcher", "role": "Finds facts in the knowledge bases",
                                       "instructions": "Search the knowledge base and report facts with source numbers.",
                                       "connection": "", "model": "", "kb_ids": [], "mcp_tools": []}]},
             "description": "A supervisor agent that delegates parts of the task to specialist member agents."},
    "final": {"label": "Direct Output", "inputs": {"answer": "draft", "evidence": "evidence"}, "outputs": {},
              "optional_inputs": ["evidence"], "defaults": {},
              "description": "Releases the answer without Answer Review. Only allowed in projects that do not require review."},
}

CONDITION_RULES = {
    "verdict_is": "reviewed",        # argument: pass | revise | needs_human_review
    "has_evidence": "evidence",      # argument ignored
    "text_contains": "text",         # argument: substring (case-insensitive)
    "draft_contains": "draft",       # argument: substring of an agent/LLM answer
}
TOOL_NAME_LIMIT = 64


def _node_errors(node: dict) -> list[str]:
    errs = []
    t = node.get("type")
    spec = CATALOG[t]
    data = node.get("data") or {}
    for key in spec.get("required", []):
        if data.get(key) in (None, "", []):
            errs.append(f"{spec['label']} '{node['id']}': setting '{key}' is required")
    if t == "retrieval":
        if not isinstance(data.get("top_k", 6), int) or not 1 <= data.get("top_k", 6) <= 20:
            errs.append(f"Retrieval '{node['id']}': top_k must be 1-20")
        if data.get("scope", "auto") not in ("auto", "fixed", "all"):
            errs.append(f"Retrieval '{node['id']}': scope must be auto, fixed or all")
        bad = [s for s in data.get("section_types") or [] if s not in SECTION_TYPES]
        if bad:
            errs.append(f"Retrieval '{node['id']}': unknown section types {bad}")
    if t == "prompt" and "{question}" not in (data.get("template") or ""):
        errs.append(f"Prompt '{node['id']}': the template must contain {{question}}")
    if t in ("llm", "review"):
        temp = data.get("temperature", 0.1)
        if not isinstance(temp, (int, float)) or not 0 <= temp <= 1.5:
            errs.append(f"{spec['label']} '{node['id']}': temperature must be 0-1.5")
        mt = data.get("max_tokens", 800)
        if not isinstance(mt, int) or not 16 <= mt <= 4096:
            errs.append(f"{spec['label']} '{node['id']}': max_tokens must be 16-4096")
    if t == "retrieval" and data.get("graph", "off") not in ("off", "local", "global", "both"):
        errs.append(f"Retrieval '{node['id']}': graph must be off, local, global or both")
    if t in ("agent", "team"):
        steps = data.get("max_steps", 6)
        if not isinstance(steps, int) or not 1 <= steps <= 15:
            errs.append(f"{spec['label']} '{node['id']}': max_steps must be 1-15")
        mt = data.get("max_tokens", 1200)
        if not isinstance(mt, int) or not 64 <= mt <= 8192:
            errs.append(f"{spec['label']} '{node['id']}': max_tokens must be 64-8192")
    if t == "team":
        members = data.get("members") or []
        names = [str(m.get("name", "")) for m in members if isinstance(m, dict)]
        if not 1 <= len(members) <= 6:
            errs.append(f"Agent Team '{node['id']}': needs 1-6 member agents")
        if len(set(names)) != len(names) or not all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]{0,30}", n) for n in names):
            errs.append(f"Agent Team '{node['id']}': member names must be unique, start with a letter, "
                        "and use letters, digits, - or _")
    if t == "review" and data.get("fallback", "extract") not in ("extract", "not_found", "human"):
        errs.append(f"Answer Review '{node['id']}': fallback must be extract, not_found or human")
    if t == "condition":
        if data.get("rule") not in CONDITION_RULES:
            errs.append(f"Condition '{node['id']}': unknown rule")
        if data.get("rule") == "verdict_is" and data.get("argument") not in ("pass", "revise", "needs_human_review"):
            errs.append(f"Condition '{node['id']}': argument must be pass, revise or needs_human_review")
    return errs


def topo_order(nodes: list[dict], edges: list[dict]) -> list[str] | None:
    ids = [n["id"] for n in nodes]
    indeg = {i: 0 for i in ids}
    out = defaultdict(list)
    for e in edges:
        out[e["source"]].append(e["target"])
        indeg[e["target"]] += 1
    order, q = [], deque(sorted(i for i in ids if indeg[i] == 0))
    while q:
        n = q.popleft()
        order.append(n)
        for t in sorted(out[n]):
            indeg[t] -= 1
            if indeg[t] == 0:
                q.append(t)
    return order if len(order) == len(ids) else None


def port_types(nodes: list[dict], edges: list[dict]) -> dict[tuple[str, str], str]:
    """Resolve output port types, including pass-through Condition ports."""
    by_id = {n["id"]: n for n in nodes}
    order = topo_order(nodes, edges) or []
    types: dict[tuple[str, str], str] = {}
    for nid in order:
        n = by_id[nid]
        spec = CATALOG[n["type"]]
        for port, t in spec["outputs"].items():
            if t == "same":
                incoming = [e for e in edges if e["target"] == nid]
                t = types.get((incoming[0]["source"], incoming[0].get("sourceHandle") or ""), "unknown") if incoming else "unknown"
            types[(nid, port)] = t
    return types


def agent_specs(node: dict) -> list[dict]:
    """The agent configurations inside an Agent or Agent Team node (supervisor first)."""
    d = {**CATALOG.get(node["type"], {}).get("defaults", {}), **(node.get("data") or {})}
    if node["type"] == "agent":
        return [d]
    if node["type"] == "team":
        base = {k: d.get(k) for k in ("connection", "model", "temperature", "max_tokens")}
        return [d] + [{**base, **{k: v for k, v in m.items() if v not in (None, "")}} for m in d.get("members") or []
                      if isinstance(m, dict)]
    return []


def validate_graph(graph: dict, *, allowed_kbs: set[str], connections: set[str],
                   mcp_tools: set[str] = frozenset(), require_review: bool = True,
                   hosted: set[str] = frozenset()) -> list[str]:
    """hosted: connections to non-Ollama providers, where the administrator's default model name does not apply."""
    errors: list[str] = []
    nodes, edges = graph.get("nodes") or [], graph.get("edges") or []
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return ["Graph must contain node and edge lists"]
    if len(nodes) > 60 or len(edges) > 120:
        return ["Graph is too large (max 60 nodes, 120 edges)"]
    ids = [n.get("id") for n in nodes]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and 0 < len(i) <= 64 for i in ids):
        return ["Node ids must be unique short strings"]
    unknown = [n.get("type") for n in nodes if n.get("type") not in CATALOG]
    if unknown:
        return [f"Unknown node type(s): {unknown}"]
    by_id = {n["id"]: n for n in nodes}
    for n in nodes:
        errors.extend(_node_errors(n))
        d = n.get("data") or {}
        if n["type"] == "retrieval":
            missing = [k for k in d.get("kb_ids") or [] if k not in allowed_kbs]
            if missing:
                errors.append(f"Retrieval '{n['id']}': you do not have access to knowledge base(s) {missing}")
        if n["type"] in ("llm", "review") and d.get("connection", "local-ollama") not in connections:
            errors.append(f"{CATALOG[n['type']]['label']} '{n['id']}': model connection '{d.get('connection')}' is not available")
        uses_model = n["type"] == "llm" or (n["type"] == "review" and d.get("model_review", True))
        if uses_model and d.get("connection", "local-ollama") in hosted and not d.get("model"):
            errors.append(f"{CATALOG[n['type']]['label']} '{n['id']}': choose a model for connection '{d.get('connection')}'")
        for spec in agent_specs(n):
            label = f"{CATALOG[n['type']]['label']} '{n['id']}'" + (f" member '{spec.get('name')}'" if spec.get("role") else "")
            if (spec.get("connection") or "local-ollama") not in connections:
                errors.append(f"{label}: model connection '{spec.get('connection')}' is not available")
            elif (spec.get("connection") or "local-ollama") in hosted and not spec.get("model"):
                errors.append(f"{label}: choose a model for connection '{spec.get('connection')}'")
            bad_kb = [k for k in spec.get("kb_ids") or [] if k not in allowed_kbs]
            if bad_kb:
                errors.append(f"{label}: you do not have access to knowledge base(s) {bad_kb}")
            bad_tools = [t for t in spec.get("mcp_tools") or [] if t not in mcp_tools]
            if bad_tools:
                errors.append(f"{label}: MCP tool(s) not enabled or not yours: {bad_tools}")
        if n["type"] == "final" and require_review:
            errors.append(f"Direct Output '{n['id']}': this project requires Answer Review; use the Output node")
    counts = defaultdict(int)
    for n in nodes:
        counts[n["type"]] += 1
    if counts["input"] != 1:
        errors.append("The workflow needs exactly one Input node")
    if counts["output"] + counts["final"] < 1:
        errors.append("The workflow needs at least one Output (or Direct Output) node")
    seen_targets = set()
    for e in edges:
        s, t = e.get("source"), e.get("target")
        sh, th = e.get("sourceHandle") or "", e.get("targetHandle") or ""
        if s not in by_id or t not in by_id:
            errors.append(f"Connection {e.get('id')} refers to a missing node"); continue
        if sh not in CATALOG[by_id[s]["type"]]["outputs"]:
            errors.append(f"Connection {e.get('id')}: '{by_id[s]['type']}' has no output '{sh}'"); continue
        if th not in CATALOG[by_id[t]["type"]]["inputs"]:
            errors.append(f"Connection {e.get('id')}: '{by_id[t]['type']}' has no input '{th}'"); continue
        if (s, sh, t, th) in seen_targets:
            errors.append(f"Duplicate connection {e.get('id')}")
        seen_targets.add((s, sh, t, th))
    if errors:
        return errors
    if topo_order(nodes, edges) is None:
        return ["The workflow contains a loop; loops are not supported"]
    types = port_types(nodes, edges)
    for e in edges:
        want = CATALOG[by_id[e["target"]]["type"]]["inputs"][e["targetHandle"]]
        got = types.get((e["source"], e["sourceHandle"]), "unknown")
        if want != "any" and got != want:
            errors.append(f"Cannot connect {by_id[e['source']]['type']}.{e['sourceHandle']} ({got}) to "
                          f"{by_id[e['target']]['type']}.{e['targetHandle']} (expects {want})")
    for n in nodes:
        spec = CATALOG[n["type"]]
        for port in spec["inputs"]:
            if port in spec.get("optional_inputs", []):
                continue
            if not any(e["target"] == n["id"] and e["targetHandle"] == port for e in edges):
                errors.append(f"{spec['label']} '{n['id']}': input '{port}' is not connected")
        if n["type"] == "condition":
            incoming = [e for e in edges if e["target"] == n["id"]]
            rule_type = CONDITION_RULES.get((n.get("data") or {}).get("rule"))
            if incoming and rule_type and types.get((incoming[0]["source"], incoming[0]["sourceHandle"])) != rule_type:
                errors.append(f"Condition '{n['id']}': rule '{n['data']['rule']}' needs a {rule_type} input")
            if len(incoming) > 1:
                errors.append(f"Condition '{n['id']}': only one input connection is allowed")
    # every node must be reachable from Input (no orphan branches that silently never run)
    reach, q = set(), deque([n["id"] for n in nodes if n["type"] == "input"])
    while q:
        cur = q.popleft()
        if cur in reach:
            continue
        reach.add(cur)
        q.extend(e["target"] for e in edges if e["source"] == cur)
    orphans = [n["id"] for n in nodes if n["id"] not in reach]
    if orphans:
        errors.append(f"Node(s) {orphans} cannot be reached from the Input node")
    return errors


def default_graph(kb_ids: list[str], model: str, review_model: str, *, template: str = "qa",
                  connection: str = "local-ollama", require_review: bool = True) -> dict:
    """Guided-setup graphs.
    qa    : Input → Retrieval → Prompt → LLM → Answer Review → Output   (document Q&A, checked)
    agent : Input → Agent (knowledge search + tools) → Answer Review → Output, or → Direct Output
    chat  : Input → Prompt → LLM → Direct Output                         (projects without mandatory review)"""
    def node(i, t, x, y, **data):
        d = dict(CATALOG[t]["defaults"]); d.update(data)
        return {"id": i, "type": t, "position": {"x": x, "y": y}, "data": d}
    E = lambda i, s, sh, t, th: {"id": i, "source": s, "sourceHandle": sh, "target": t, "targetHandle": th}
    conn = connection or "local-ollama"
    if template == "agent":
        nodes = [node("input", "input", 0, 150), node("agent", "agent", 240, 150, kb_ids=kb_ids, connection=conn, model=model)]
        edges = [E("e1", "input", "question", "agent", "question")]
        if require_review:
            nodes += [node("review", "review", 520, 150, connection=conn, model=review_model), node("output", "output", 780, 150)]
            edges += [E("e2", "agent", "answer", "review", "draft"), E("e3", "agent", "evidence", "review", "evidence"),
                      E("e4", "input", "question", "review", "question"), E("e5", "review", "reviewed", "output", "answer")]
        else:
            nodes += [node("output", "final", 520, 150)]
            edges += [E("e2", "agent", "answer", "output", "answer"), E("e3", "agent", "evidence", "output", "evidence")]
        return {"nodes": nodes, "edges": edges}
    if template == "chat":
        nodes = [node("input", "input", 0, 150),
                 node("prompt", "prompt", 240, 150, system="You are a helpful assistant.", template="{question}"),
                 node("llm", "llm", 480, 150, connection=conn, model=model), node("output", "final", 720, 150)]
        edges = [E("e1", "input", "question", "prompt", "question"), E("e2", "prompt", "prompt", "llm", "prompt"),
                 E("e3", "llm", "draft", "output", "answer")]
        return {"nodes": nodes, "edges": edges}
    nodes = [node("input", "input", 0, 150), node("retrieve", "retrieval", 215, 0, kb_ids=kb_ids),
             node("prompt", "prompt", 430, 150), node("llm", "llm", 645, 150, connection=conn, model=model),
             node("review", "review", 860, 150, connection=conn, model=review_model), node("output", "output", 1075, 150)]
    edges = [E("e1", "input", "question", "retrieve", "question"), E("e2", "input", "question", "prompt", "question"),
             E("e3", "retrieve", "evidence", "prompt", "evidence"), E("e4", "prompt", "prompt", "llm", "prompt"),
             E("e5", "llm", "draft", "review", "draft"), E("e6", "retrieve", "evidence", "review", "evidence"),
             E("e7", "input", "question", "review", "question"), E("e8", "review", "reviewed", "output", "answer")]
    return {"nodes": nodes, "edges": edges}


def connection_names(conn: sqlite3.Connection, owner: str | None = None) -> set[str]:
    from .llm import usable_connection_names
    return usable_connection_names(conn, owner)


def usable_mcp_tools(conn: sqlite3.Connection, owner: str | None) -> set[str]:
    rows = conn.execute("""SELECT t.server_id || '/' || t.name FROM mcp_tools t JOIN mcp_servers s ON s.id=t.server_id
        WHERE t.enabled=1 AND s.enabled=1 AND (s.owner_user_id IS NULL OR s.owner_user_id=?)""", (owner or "",))
    return {r[0] for r in rows}


def graph_context(conn: sqlite3.Connection, actor_id: str, project_id: str) -> dict:
    from .policy import readable_kbs
    p = conn.execute("SELECT require_review FROM projects WHERE id=?", (project_id,)).fetchone()
    names = connection_names(conn, actor_id)
    hosted = {r[0] for r in conn.execute("SELECT name FROM model_connections WHERE kind != 'ollama'")} & names
    return {"allowed_kbs": readable_kbs(conn, actor_id, project_id), "connections": names,
            "mcp_tools": usable_mcp_tools(conn, actor_id), "require_review": bool(p[0]) if p else True, "hosted": hosted}
