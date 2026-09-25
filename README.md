# NexAgent Studio 0.3

A no-code / low-code **agent builder**. People upload documents, build agents on a visual canvas, connect **public LLMs** (OpenAI, Anthropic, Google Gemini, any OpenAI-compatible service) or local Ollama models, give agents **MCP tools**, combine them into **multi-agent teams**, and use **GraphRAG** knowledge graphs for retrieval. Answers cite their sources and are checked against them.

It comes in two editions that share one design:

| | **NexAgent Studio** (server) | **NexAgent Lite** (browser-only) |
|---|---|---|
| Where it runs | Your server or a cloud host (Render, Railway, Hugging Face Spaces, a VM) | Any static host, e.g. **GitHub Pages**; the entire app runs in the visitor's browser |
| Users | Accounts; open sign-up in public mode; private workspaces; admin screens | One person per browser, no accounts |
| API keys | Each user adds their own keys, encrypted on the server (or admin-shared connections) | Kept in the visitor's browser only |
| Data | SQLite on the server's disk | IndexedDB in the browser; export/import as JSON |
| Builder | Visual canvas: Retrieval, Prompt, LLM, **Agent**, **Agent Team**, Answer Review, Condition, Output nodes | Form-based agent and team builder with a live diagram |
| MCP tools | Remote (Streamable HTTP); local-command (stdio) for admins on private servers | Remote servers that allow browser access (CORS) |
| GraphRAG | Rules or LLM extraction, Louvain communities, local/global retrieval, graph viewer | Same, in the browser |
| Answer checks | Full Answer Review (citations, equipment↔value binding, units, limits, negation…) with optional engineer queue | Citation, number, unit and equipment-tag checks |
| Plant features | Engineer review queue, fine-tuning (LoRA/QLoRA) with approvals — for private (non-public) servers | — |

## Try it

**Lite (no install):** after you publish the repository, NexAgent Lite is at `https://<your-user>.github.io/<repo>/`. Open it, add an API key under **API keys & data**, then create a knowledge base, an agent and chat. See [lite/README.md](lite/README.md).

**Server, local test:**
```bash
scripts/install.sh                                     # creates ./venv, ./.env and the database
echo "NEXAGENT_PUBLIC_MODE=1" >> .env                  # open sign-up + bring-your-own keys
scripts/run.sh start                                   # http://127.0.0.1:8600 — "Create an account"
```

**Server, public deployment:** push this repository to GitHub, then deploy the Dockerfile on Render (blueprint `render.yaml`), Railway (`railway.json`), Hugging Face Spaces or any Docker host. Step-by-step instructions: [docs/DEPLOY_PUBLIC.md](docs/DEPLOY_PUBLIC.md).

**Server, private plant installation** (JupyterHub/GPU server, local Ollama, no Docker): [docs/INSTALL.md](docs/INSTALL.md). The earlier plant workflow, with engineer review and fine-tuning, is unchanged.

## What a builder can make

- **Knowledge Q&A with checks:** Retrieval → Prompt → LLM → Answer Review → Output. Every value in an answer must appear in the cited passage.
- **Tool-using agent:** an Agent node decides when to search the knowledge bases (with or without GraphRAG) and when to call MCP tools such as web search, a database or a ticket system.
- **Agent team:** a leader agent delegates parts of a task to up to six specialist members. Each member has its own model, instructions, knowledge bases and tools. You can mix providers, for example a Claude leader with a Gemini researcher.
- **Simple chat:** Prompt → LLM → Direct Output (only in projects that do not require review).

## Safety properties (both editions)

- Keys are never shown again after saving (server) and never leave the browser except to the provider they belong to (Lite).
- MCP tools start **disabled**; users enable each tool they trust. Tool output is passed to models as data, never as instructions.
- Server edition:
  - content classification (Public/Internal/Restricted) is checked **before** anything is sent to a model provider or MCP server;
  - personal endpoints must be public `https` addresses, re-checked at call time (SSRF protection);
  - local-command MCP servers are admin-only and are disabled in public mode.
- GraphRAG topic summaries are generated text. The answer checks never accept a summary as the only source for a specific value.

Details: [SECURITY.md](SECURITY.md).

## Repository layout

```
backend/nexagent/          FastAPI app: providers, MCP client, agents, GraphRAG, executor, validators, review, training
backend/nexagent/static/   pre-built web interface (no Node.js needed on a server)
backend/tests/             81 automated tests (fake OpenAI/Anthropic/Gemini, real MCP SDK server, real LoRA run)
frontend/                  React + React Flow source of the server UI
lite/                      NexAgent Lite (browser-only edition) + its unit tests
scripts/                   install/run, browser journeys: e2e_browser.py (plant), e2e_public.py, e2e_lite.py
.github/workflows/         ci.yml (tests + builds), pages.yml (publishes Lite to GitHub Pages)
Dockerfile, render.yaml, railway.json   container deployment
docs/                      install, public deployment, user, admin, architecture guides; screenshots; test evidence
evaluations/               50 golden cases + scoring script
samples/                   fictional documents (not real plant data)
```

## Honest status

[STATUS.md](STATUS.md), [TEST_RESULTS.md](TEST_RESULTS.md) and [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) describe what was verified and how.

- **Verified here:**
  - every provider's request and reply format, against test doubles of the OpenAI, Anthropic and Gemini APIs;
  - MCP, against a real MCP SDK server;
  - the full browser journeys of both editions.
- **Not verified here:**
  - calls to the real OpenAI, Anthropic or Gemini services (no keys were available);
  - the Docker image build (the container registry was blocked). CI builds the image on every push.

Answers in the screenshots are scripted.
