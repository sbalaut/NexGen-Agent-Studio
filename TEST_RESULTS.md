# Test results — v0.3.0 (25 September 2026)

All results below were actually executed. Logs are in `docs/test-evidence/` (files ending in `-v0.3`).

| Check | Result | Evidence |
|---|---|---|
| Backend suite, Python 3.11, with the training environment | **81 passed, 0 failed, 0 skipped** (includes 2 real LoRA training runs) | backend-v0.3-py311.txt |
| Server UI: TypeScript check + production build | no type errors; 467 kB JS (145 kB gzipped) | frontend-v0.3-build.txt |
| NexAgent Lite: unit tests + type check + build | **17 passed**; build OK | lite-v0.3.txt |
| Browser journey, plant mode (unchanged v0.2 journey) | **completed, 0 console errors**, screenshots 01–14 | e2e_browser-v0.3.txt |
| Browser journey, public mode: sign-up → own API key → MCP server + tool → KB with GraphRAG → tool-using agent → test run (the MCP result verifiably reached the model) → agent team | **completed, 0 console errors**, screenshots p01–p06 | e2e_public-v0.3.txt |
| Browser journey, NexAgent Lite (built site): key → .md + .pdf + .docx upload → index + graph → search → MCP tools → agent team → cited answer "checks passed" → unsupported value flagged → reload keeps data → mobile | **completed, 0 console errors**, screenshots l01–l06 | e2e_lite-v0.3.txt |
| Clean copy as GitHub would receive it (`git add .` = 170 files, no data/keys/node_modules), fresh Python 3.11 venv from `requirements.lock.txt` + `requirements-dev.txt` (what CI does) | **79 passed, 2 skipped** (the 2 real-training tests need a training venv); Lite `npm ci && npm test && npm run build` passed | reproduced during build |
| Container start command (`PORT` from the host, public mode, admin bootstrap from env) | health and config endpoints answered; admin created once | reproduced during build |

## What the new tests prove (0.3)

**Providers:**
- The exact wire format for OpenAI (official `max_completion_tokens`; compatible services use `max_tokens`), Anthropic (system split, `tool_use`/`tool_result` blocks, browser header in Lite) and Gemini (`functionCall`/`functionResponse`, schema cleaning, batch embeddings).
- A rejected key gives a clear message.

**SSRF:** personal endpoints resolving to private, loopback or link-local addresses are refused, both when saved and when called.

**MCP:**
- Real MCP SDK server over stdio and Streamable HTTP: initialize, tools/list, tools/call.
- Tools start disabled.
- A tool call is **blocked, and nothing is sent**, when the conversation's classification exceeds the server's ceiling.
- A question in a Public workspace starts as Public on a public server; on a plant server it never starts below Internal.

**Agents:**
- A single agent uses knowledge search and MCP tools.
- A team leader delegates to members that use their own tools.
- Source numbers stay consistent across agents.
- Loops are bounded.
- A hosted-provider connection without an explicit model fails validation.

**GraphRAG:**
- Rules and LLM extraction both work.
- Communities are reproducible (seeded).
- Local and global retrieval both work.
- A generated summary cited as the only source for a value is flagged `summary_not_primary_source`.

**Public mode:**
- Sign-up is rate-limited per address.
- Each user gets a private workspace.
- Run, document and upload quotas are enforced.
- The user directory is hidden.
- Review and training routes return 404.
- A v3 database migrates to v4 and keeps its connections.
- The first admin can be created from environment variables, exactly once.

---

## Earlier results (v0.2.0)

All results below were actually executed. Logs are in `docs/test-evidence/`.

| Check | Result | Evidence |
|---|---|---|
| Backend suite, Python 3.11, with training env | **56 passed, 0 failed** (includes 2 real LoRA training tests) | backend-py311.txt |
| Backend suite, Python 3.11 after the DHDT-model update (no training env) | **58 passed, 2 skipped**; earlier on Python 3.10: **54 passed, 2 skipped** (training tests need `TEST_TRAIN_PYTHON`) | backend-py310.txt |
| Frontend TypeScript check + production build | no type errors; build OK (435 kB JS, 136 kB gzipped) | frontend-build.txt |
| Browser journey (Playwright + Chromium) against the real server, workers and SQLite | **completed, 0 console errors**, 14 screenshots | browser-journey.txt, docs/screenshots/ |
| Clean install from a fresh copy (`scripts/install.sh`, `create-admin`, `run.sh start`, login over HTTP, `run.sh check`) | worked; `check` reported the missing Ollama explicitly | reproduced during build |
| URL-prefix mode (`NEXAGENT_BASE_PATH=/services/nexagent`) | API, UI and redirect verified | reproduced during build |

## What the automated tests prove

**Security and access:** a per-account lockout does not block other accounts; lock times grow exponentially and are capped; CSRF and Origin checks are enforced; CSP and frame headers are set. A project member **without** a knowledge-base grant gets "not found", and *no source text reaches the model*. Outsiders cannot see runs, assistants or the source viewer. **Restricted content is blocked before sending** to an Internal-only endpoint (the fake server received zero chat requests). API keys are stored as verifiers only, and revocation blocks access to past results. The audit log rejects deletes.

**Documents and search:** table rows keep equipment, value and condition together; section types are detected; scanned PDFs are reported; a failed re-index keeps the previous index active; "describe the process" questions retrieve only process-description sections.

**Builder and executor:** the graph cannot bypass Answer Review (typed ports); cycles, unknown nodes and inaccessible knowledge bases are rejected; conflicting edits give 409; a correctly cited answer is released with server-built citations, and exactly one source ref out of all retrieved passed; one repair attempt followed by a re-check works; a swapped value goes to an engineer and **the draft never appears in the event stream**; an unavailable or unparseable reviewer sends the answer to a human; cancellation discards a late model response; a missing model gives an explicit `ollama pull` message; a run interrupted by a restart is marked failed with a clear message.

**Validators (21 cases):** swapped equipment values, invented tags, invalid citations, uncited claims, wrong unit/value/sign, startup-vs-normal and trip-vs-normal confusion, reversed inequality, dropped negation, out-of-scope section, mixed revisions, prompt-injection fencing, non-blocking language mismatch.

**Review, datasets and leakage:** a thumbs-up creates no training data; corrections need citations; decisions are immutable; the two-person rule applies; editing invalidates approval; group splits must be explicit; minimum split sizes apply; the export has train + validation with hash and classification and **no held-out rows**; manifests are immutable; deleting a document restricts dataset versions; an operator without knowledge-base access cannot see the examples.

**Training (real, not simulated):** a tiny random-weight Llama was built offline. The full chain ran: request → blocked self-approval → approval by a second operator → preflight → real LoRA training in a separate process → metrics ingested from the runner log → adapter registered with SHA-256 hashes → held-out file absent from the job folder → evaluation of base vs candidate → promotion refused for the requester → promotion by the promoter → export **failed honestly** because no Ollama CLI existed, and nothing was switched. A second test cancelled a running job mid-training, and it ended as `cancelled` with checkpoints kept. A preflight with an incomplete model folder produced `blocked` with the exact failed checks and zero metrics.

**Other:** alias pinning and one-step rollback; the separate promotion permission; golden-suite composition (50 cases: answerable, unanswerable, Hindi, Hinglish, adversarial) and its scoring rules; the model-evaluation scorer penalises abstaining; backup → restore into an empty directory with an integrity check.

## Not tested here (no hardware or models available)

- Answer quality with a real LLM. Run `evaluations/run_golden.py` on your server.
- GPU training, QLoRA/bitsandbytes, and training a real base model.
- `ollama create` import of a merged model and the success path of promotion.
- Real JupyterHub service routing (prefix handling itself is tested), direct TLS with a real certificate, and load or performance.
