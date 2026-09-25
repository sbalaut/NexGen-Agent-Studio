# Status — v0.3.0

## New in 0.3 (public edition)

| Area | Status | Notes |
|---|---|---|
| Public LLM providers: OpenAI, Anthropic, Gemini, OpenAI-compatible (server + Lite) | **Implemented, tested against API test doubles** | Tool calling, JSON mode and embeddings for each provider. No calls to the real services were possible here. |
| Bring-your-own API keys, encrypted per user (server) | **Implemented, tested, browser-verified** | Personal endpoints must be public https (SSRF check at save and call time) |
| Public mode: sign-up, private workspace, quotas, hidden directory, feature flags | **Implemented, tested, browser-verified** | Engineer review and training are hidden in public mode |
| MCP client (Streamable HTTP + stdio), per-tool enable, classification ceiling | **Implemented; tested against a real MCP SDK server** | stdio is admin-only and off in public mode |
| Agent node, Agent Team node (leader + up to 6 members), Direct Output | **Implemented, tested, browser-verified** | Bounded loops, cancellation, trace |
| GraphRAG: rules/LLM extraction, Louvain communities, local/global retrieval, graph viewer | **Implemented, tested, browser-verified** | Summaries can never be the only source for a value |
| Builder UI for agents/teams/MCP/graph modes; KB index settings; guided templates | **Implemented, browser-verified** | |
| NexAgent Lite (browser-only, GitHub Pages) | **Implemented; 17 unit tests + browser journey** | Real pdf.js / mammoth / graphology in the browser |
| GitHub packaging: CI, Pages workflow, Dockerfile, Render/Railway configs, env-based admin bootstrap | **Delivered** | The container start command was verified locally. The image build itself was not (the registry was blocked here); CI builds it on every push. |

## Plant features from 0.2 (unchanged, still tested)

| Area (spec section) | Status | Notes |
|---|---|---|
| Identities, roles, sessions, CSRF, throttling (4) | **Implemented, tested** | Five roles, combinable, plus a separate promotion permission; local accounts only |
| Projects, membership, KB grants, classification (4, 6) | **Implemented, tested** | SQLite: authorization in code + SQL, no RLS |
| Documents: PDF/DOCX/TXT/MD, preview, section labels, revisions, atomic index generations (6) | **Implemented, tested** | No OCR (scanned PDFs are reported) |
| Hybrid retrieval FTS5 + vectors + RRF, scope rules (3, 6) | **Implemented, tested** | Brute-force vectors |
| Model gateway: classification ceiling, allow-list, no redirects, encrypted keys (2, 11) | **Implemented, tested** | |
| No-code builder: canvas, palette, typed ports, undo/redo, versions, list/form view, validation (5) | **Implemented, browser-verified** | |
| Deterministic DAG executor, conditions, skip/merge, cancellation, SSE (5) | **Implemented, tested** | |
| Answer Review Agent: validators + model reviewer + one repair + fallback (7) | **Implemented, tested with test double** | Real-model quality to be measured on site |
| Engineer review queue with history (7) | **Implemented, tested, browser-verified** | |
| Feedback, candidates, prep suggestions, approvals, immutable versions, group splits (8) | **Implemented, tested** | Suggestions are rule-based |
| Training coordinator, preflight, runner, metrics, cancel, resume, artifacts (9) | **Implemented; real CPU LoRA run verified** | GPU/QLoRA not verified here |
| Evaluation base vs candidate, deterministic scores (10) | **Implemented; run on the test model** | |
| Promotion (separate permission), Ollama export, alias switch, rollback, run pinning (10) | **Implemented; failure path verified** | Success path needs Ollama on site |
| Publishing, private chat, API keys, rollback (5, 6) | **Implemented, tested, browser-verified** | |
| 50 frozen golden cases + runner (12) | **Delivered** | Must be run against your models |
| Install without Docker, offline wheelhouse, backup/restore, guides (12, 13) | **Delivered; clean install and restore verified** | |
| Deferred | — | OCR, SSO/LDAP, HA, HTTP/action nodes, loops, schedules, plant connectors |

**Default models:** qwen3-next:80b-a3b-instruct-q4_K_M (answers + review) and qwen3-embedding:8b (search), the same as the DHDT chatbot.

**Next step on the MRPL server:** install, then pull the models and run the golden suite. After that, index one real manual in a restricted pilot project with 3–5 engineers.
