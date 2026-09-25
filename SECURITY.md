# Security notes

**Deployment model:** one server inside the plant network. The app listens on 127.0.0.1 unless configured otherwise. HTTPS comes from the JupyterHub proxy, a reverse proxy, or uvicorn's own TLS (`NEXAGENT_TLS_CERT/KEY`). Plain HTTP on a non-loopback address is refused unless it is explicitly allowed.

| Area | Control |
|---|---|
| Passwords | Argon2id (t=3, m=64 MiB, p=2), at least 12 characters, rehashed on login when parameters change; no default accounts; first admin created interactively |
| Sessions | 48-byte random token, stored only as SHA-256, HttpOnly + SameSite=Strict (+Secure on HTTPS), 8 h default, all revoked on role change, disable or password reset |
| CSRF | HMAC(session) token in a header on every state-changing request, plus an Origin/Referer check |
| Brute force | Per-account and per-address exponential lockout; no site-wide lock |
| Authorization | Central `policy.py`; project membership + explicit knowledge-base grants; enforced in routes, **inside retrieval SQL**, again at answer release, and in workers; removing a member drops their KB grants (DB trigger) |
| Classification | Public / Internal / Restricted on projects, KBs, documents and chunks; derived answers, review items, datasets and adapters inherit the maximum |
| Model egress | Every call goes through `llm.Gateway`: per-connection classification ceiling checked **before sending**; host allow-list with a DNS resolution check; redirects disabled; `trust_env=False` so no system proxy is used; external connections can never receive Restricted content |
| Secrets | `data/secret.key` (sessions, CSRF, API-key verifiers) and `data/connections.key` (Fernet encryption of endpoint API keys) live outside the database, mode 0600; never returned to the browser |
| API keys | Scoped to one assistant, its knowledge bases and a classification ceiling; only an HMAC verifier is stored; revoking a key cancels its running requests and blocks access to its past results |
| Uploads | Extension allow-list, size limit, SHA-256, immutable revisions; parsing in a separate process with a timeout; scanned PDFs reported, not OCR'd |
| Output | Answers are rendered as plain text (never HTML); drafts are never streamed to users |
| Prompt injection | Sources are fenced as untrusted data. Built-in validators and access rules do not depend on any model; a test source containing an injection is part of the golden suite |
| Audit | Append-only table (DB triggers reject UPDATE/DELETE) covering auth, permissions, uploads, indexing, publishing, reviews, dataset approvals, training, promotion, rollback |
| Training | Separate process and Python environment, proxy variables removed, offline Hugging Face flags, base models only from the approved folder, allow-listed recipes with bounded parameters, no execution of generated code, one job at a time, time limit, optional dedicated GPU |
| Headers | CSP (`default-src 'self'`, no inline scripts), X-Frame-Options DENY, nosniff, Referrer-Policy, Permissions-Policy, HSTS on HTTPS |

## Public mode and public providers (v0.3)

| Area | Control |
|---|---|
| Sign-up | Off unless `NEXAGENT_PUBLIC_MODE=1` (or `NEXAGENT_ALLOW_SIGNUP=1`); limited per address per day; new users get Builder + Viewer only and a private workspace |
| Personal API keys | Fernet-encrypted at rest, never returned to the browser, usable only by their owner; Public/Internal ceiling only, **never Restricted**. The operator of a public server could technically use stored keys, and the app says so where keys are entered |
| Personal endpoints (SSRF) | Must be `https` and resolve only to public (global) IP addresses; checked when saved **and** before every call (DNS can change); redirects disabled |
| MCP | Tools start disabled; each call re-checks enablement, ownership and the server's classification ceiling before anything is sent; results are passed back as untrusted data; stdio (local command) servers are admin-only, shared-only, and disabled in public mode; non-admins never see shared servers' URLs or commands |
| Agents | Bounded steps (≤ 15), bounded tool calls per step, cancellation after every call, and knowledge search through the same permission-aware retrieval as the Retrieval node |
| GraphRAG | Graph entities come from permitted passages only; generated summaries are marked as such and cannot be the sole citation for a value |
| Quotas | Runs per hour, documents per user and upload megabytes per user, all configurable |
| Review | Projects in public mode may skip Answer Review (Direct Output). Plant projects require review unless an administrator decides otherwise |

**NexAgent Lite (browser-only):** keys and data never leave the browser except in calls to the provider or MCP server the user added. A strict Content-Security-Policy is set (no third-party scripts, no `eval`, `connect-src` limited to https and localhost). Exports exclude keys and MCP auth headers. Anyone with access to the same browser profile can read the stored keys, and the Settings page says so.

**Known gaps (accepted for a single-server pilot):** SQLite has no row-level security, so a code bug in a route could bypass a check that PostgreSQL RLS would have caught (mitigated by the central policy module and the tests). There is no SSO/LDAP. The training process's network isolation depends on server egress rules, since removing proxy variables does not block direct connections. There is no automatic data-retention purge.

Report problems to the MRPL IT application owner.
