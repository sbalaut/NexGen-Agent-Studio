# NexAgent Lite — the browser-only edition

NexAgent Lite is a static web app. It needs no server, no database and no account, so it can be hosted for free on **GitHub Pages**. Everything runs in the visitor's browser:

- **API keys:** OpenAI, Anthropic (Claude), Google Gemini, or any OpenAI-compatible service. Keys live in the browser's session storage, or local storage if "Remember keys" is ticked.
- **Knowledge bases:**
  - `.pdf` (pdf.js), `.docx` (mammoth), `.md` and `.txt` are read in the browser and split into passages;
  - search is hybrid: BM25 keywords plus embeddings, fused with reciprocal-rank fusion;
  - everything is stored in IndexedDB.
- **GraphRAG:**
  - entities come from rules (equipment tags, headings) or from LLM extraction;
  - communities are found with Louvain (graphology, seeded so results repeat) and summarised as topics;
  - retrieval can use local (linked passages) or global (topic summaries) graph search;
  - a graph viewer is included.
- **MCP tools:** remote MCP servers over Streamable HTTP. Every tool starts disabled until you switch it on.
- **Agents:** a single tool-using agent, or a **team** where a leader delegates to up to six specialist members. Members can use different providers and models.
- **Answer checks:**
  - every cited source must exist;
  - numbers with units and equipment tags must appear in the cited passage;
  - factual sentences need a citation;
  - generated topic summaries never count as a primary source.
- **Export / import:** the whole workspace goes to a JSON file. API keys and MCP auth headers are never included. Single agents can be downloaded and shared.

## Run locally

```bash
cd lite
npm ci
npm run dev        # http://localhost:5173
npm test           # 17 unit tests (providers, MCP, ingest, search, GraphRAG, agents, checks)
npm run build      # static site in lite/dist
```

The browser journey (Playwright; model APIs and MCP server are mocked) is `python scripts/e2e_lite.py`. Run it after `npm run build`.

## Publish on GitHub Pages

The workflow `.github/workflows/pages.yml` builds and publishes `lite/dist`. Set **Settings → Pages → Source: GitHub Actions** once. The site uses relative paths, so it works under `https://<user>.github.io/<repo>/`. Details: [docs/DEPLOY_PUBLIC.md](../docs/DEPLOY_PUBLIC.md).

## Things to know

- **Direct browser calls:**
  - OpenAI and Gemini accept calls from browsers;
  - Anthropic requires the `anthropic-dangerous-direct-browser-access` header, which Lite sends;
  - many OpenAI-compatible services and most self-hosted servers must be configured to allow your site's origin (CORS), or the call fails with a network error.
- **MCP servers must allow browser access.** The server must use https (http only for `localhost`). Its CORS settings must allow your Pages origin, allow the request headers `Content-Type`, `Accept`, `Mcp-Session-Id` and `MCP-Protocol-Version` (plus any auth header you use), and expose `Mcp-Session-Id`. Local-command (stdio) MCP servers cannot run in a browser; use the server edition.
- **Security of keys:** anyone with access to the browser profile can read keys stored there. Do not use Lite on shared computers, and set spending limits with your providers. The site's Content-Security-Policy blocks third-party scripts, and there is no analytics or tracking.
- **Storage limits:** browsers give each site a storage quota, often several hundred MB or more. Very large document collections belong in the server edition.
- **Checks are heuristics.** "checks passed" means the numbers, tags and citations matched the sources. It does not prove the answer is correct.
