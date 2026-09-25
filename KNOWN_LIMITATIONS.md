# Known limitations (v0.3)

## Public edition
1. **No real calls to OpenAI, Anthropic or Gemini were possible while building this**, because no keys were available. Every adapter was tested against a test double that checks the exact request format. The request formats follow the public API documentation as of mid-2026. If a provider changes its API, the relevant adapter in `backend/nexagent/providers.py` / `lite/src/lib/providers.ts` must be updated.
2. **Suggested model names** (e.g. `gpt-4.1-mini`, `claude-sonnet-4-5`, `gemini-2.5-flash`) are only placeholders. Use **Test & list models** to see what your key offers.
3. **The Docker image was not built here**, because the container registry was blocked. CI builds it on every push. The start command, port handling and admin bootstrap were verified directly.
4. **Free hosting tiers have no persistent disk**, so data is lost on restart. Use a disk or volume at `/data`.
5. **Public mode is single-server SQLite.** It is fine for small groups (tens of concurrent users). Heavy public traffic needs PostgreSQL and a separate job queue.
6. **LLM-mode GraphRAG costs tokens.** Each group of 4 passages is one model call, capped at 600 passages on the server and 300 in Lite. Summaries are generated text and may be imprecise; they only help find sources.
7. **MCP:** only the tools capability is used (no resources, prompts or sampling). OAuth-protected MCP servers need a static bearer token in the auth header. In Lite, the MCP server must allow browser access (CORS).
8. **Lite checks are a subset** of the server's Answer Review: citations, numbers with units, equipment tags and summary-only citations. There is no engineer queue.

## From v0.2 (still valid)

1. **No real language model was available while building this.** Registry downloads of Ollama and Hugging Face models were blocked in the build environment. The executor, validators, routing and UI ran against a scripted Ollama *test double*, so answer quality with your models is **not yet measured**. Run `evaluations/run_golden.py` on your server first.
2. **Training was verified on CPU with a tiny random-weight test model**, which proves the mechanics: preflight, approval, real LoRA steps, metrics, adapter hashes, cancellation, evaluation and merge. No GPU run, QLoRA (bitsandbytes) run or real-model training was possible here.
3. **Deployment to Ollama** (`ollama create` from the merged safetensors) was not executed here. It depends on your Ollama version supporting the model architecture. Failures are reported and nothing is switched.
4. **Evaluation compares base vs base+adapter under the runner**, not the Ollama-served model. After promotion, re-run the golden suite against `promoted:<name>` (the audit entry reminds you).
5. **The validators are conservative heuristics** for English and Devanagari text with common refinery units. Unusual unit spellings, values written in words, or tables split badly by a PDF can send correct answers to engineers. Check the extraction preview.
6. **PDF extraction** uses pypdf text only. Complex multi-column layouts and tables inside PDFs may come out as plain text lines; Word or Markdown sources give the best tables. There is no OCR.
7. **Vector search is brute-force** (NumPy over authorized chunks). This is fine up to roughly 100k passages per query scope, beyond which a vector index is needed.
8. **SQLite, single server.** There is no row-level security, no high availability and no horizontal scaling. For hundreds of simultaneous users, move to PostgreSQL.
9. **No SSO/LDAP**; accounts are local.
10. **Hindi support** covers the UI copy, Devanagari text in search, and validator language checks. Hindi answer quality depends entirely on the chosen model.
11. **Accessibility**: keyboard-usable list/form alternative to the canvas, labelled controls and focus styles. There has been no formal WCAG audit.
12. Deleting a document restricts dependent datasets and adapters; it cannot remove what a trained model has already learned.
