# Architecture

```
Browser (React + React Flow, pre-built static files)
   │  same-origin HTTPS, session cookie (HttpOnly, SameSite=Strict) + CSRF token
   ▼
FastAPI app (one process)  ── policy.py: every project/KB decision, read from the DB on each check
   ├─ api/*          routes; Pydantic models reject unknown fields
   ├─ jobs.py        durable job table in SQLite: leases, bounded retries, restart recovery
   │   ├─ document.parse   → documents.py in a *separate process* with a timeout (parser isolation)
   │   ├─ kb.index         → retrieval.build_generation (atomic swap of index generations)
   │   ├─ run.execute      → executor.py (deterministic DAG) → llm.Gateway → Ollama
   │   └─ training.* / evaluation.run / deployment.export → coordinator → runner.py (separate venv/process)
   ├─ SQLite (WAL)   users, projects, grants, documents/revisions, chunks + FTS5 + vectors, workflows,
   │                 runs + event log (SSE), review items/decisions, datasets, training, deployments, audit
   └─ data/          uploads (immutable revisions), secret.key, connections.key, training folders
```

## Security boundaries
- **Authorization** is checked on the server for every route, and again *inside* both the keyword and vector search SQL. Only chunks from the active index of knowledge bases the caller can read *now*, from documents that are not deleted, are loaded. Just before an answer is released the grants are checked again, so a revocation during a run blocks the release.
- **Model gateway** (`llm.py`): every model call goes through it. It enforces the connection's classification ceiling before any bytes are sent, host allow-listing with a DNS resolution check, and no redirects. A missing model or service gives an explicit error, never a mock.
- **Untrusted documents**: sources are fenced as `<source>` data, and prompts tell both models to ignore instructions inside them. The built-in validators and access checks do not depend on the model at all.
- **No unvalidated streaming**: server-sent events carry progress only. The draft is stored for builders and reviewers, never streamed to end users.
- **Separation of duties**: an answer reviewer ≠ the dataset approver for that example; a training requester ≠ the training approver; the promoter needs a separate permission and ≠ the requester.
- **Training isolation**: a separate Python environment and process, proxy variables removed, `HF_HUB_OFFLINE=1`, base models only from the approved folder, allow-listed recipes, bounded parameters, and no generated code is ever executed.

## Answer review, deterministic part (validators.py)
For each sentence of the draft:
- citations must point to retrieved sources;
- plant-specific sentences (containing tags or values) must be cited;
- every number+unit must appear in a cited source **in the same table row or sentence as the same equipment tag**, with the same unit, sign, range, limit direction (max/min, >/<) and negation;
- values that the source gives for startup, shutdown, trip or design conditions must be stated with that condition;
- cited sections must match the question's scope, and one document may not be cited in two revisions;
- the answer language must match the question language (warning only).

The model reviewer adds findings but can only make the verdict stricter. If it is unavailable or its reply cannot be parsed, the answer goes to an engineer.

## v0.3 additions

```
llm.Gateway ──► providers.py  (ollama | openai | openai-compatible | anthropic | gemini; neutral message + tool format)
      ▲            ▲ classification ceiling + SSRF check (personal endpoints: https + global IPs, at save and call time)
      │
agents.AgentRuntime ──► search_knowledge (retrieval.retrieve, permission-aware, optional graph mode)
      │             ──► mcp_client (Streamable HTTP / stdio; tool enabled + owner + classification re-checked per call)
      │             ──► delegate_to_<member> (Agent Team; depth 1, shared evidence numbering)
executor: node_agent / node_team / node_final (Direct Output only where project.require_review = 0)

graphrag.build_graph (per index generation, before atomic activation):
  rules (tags, headings, co-occurrence) [+ LLM extraction] → networkx Louvain (seed 42) → community summaries (+ embeddings)
retrieval.retrieve(graph = off | local | global | both):
  local  → passages mentioning question entities and their 1-hop neighbours join the RRF fusion
  global → top community summaries added as evidence of kind "graph_summary" (never a primary source)
```

Public mode (`NEXAGENT_PUBLIC_MODE=1`) is configuration, not a fork:
- sign-up, a personal workspace and quotas;
- the `review`/`training` routers are unmounted via feature flags;
- stdio MCP is disabled.

Schema v4 adds:
- personal connections: `model_connections.owner_user_id`;
- `mcp_servers` and `mcp_tools`;
- `graph_entities`, `graph_relations`, `graph_mentions` and `graph_communities`;
- `usage_events`;
- `projects.require_review`.

**NexAgent Lite** (`lite/`) re-implements the same pipeline in the browser, with no backend:
- `providers.ts` and `mcp.ts`;
- `ingest.ts`, using pdf.js and mammoth;
- `search.ts`: BM25 + cosine + RRF;
- `graph.ts`: graphology + Louvain;
- `agents.ts` and `validate.ts`.

Everything is stored in IndexedDB (`store.ts`).
