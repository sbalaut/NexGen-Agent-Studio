# NexAgent Studio — guide for engineers (no programming needed)

## Everyone: asking questions
1. Open **Assistants** and pick an assistant on the left.
2. Type a question in English, Hindi or Hinglish, then press **Ask**.
3. You will see one of four results:
   - **completed**: the answer passed the checks. Each **[1]**, **[2]** is a clickable source showing the file, revision, section and the exact passage.
   - **not found**: none of the documents *you* may read contains the answer. The assistant never guesses.
   - **engineer review**: the answer could not be confirmed automatically, so an engineer is checking it. You may see relevant source extracts, quoted word for word, in the meantime. The engineer's answer appears under **Your recent questions** when it is ready.
   - **failed**: a service problem, such as the model server being down. The message says what is wrong.
4. **Yes / No** tells the team whether an answer was useful. A rating never trains a model on its own.

The assistants read documents only. They never operate plant equipment.

## Builders: making an assistant
1. **Projects → New project**. Choose the lowest classification allowed in it (Public / Internal / Restricted).
2. **Knowledge bases → New knowledge base**, then **Add documents** (text PDF, Word, .txt, .md). Scanned PDFs are flagged. OCR is not included, so upload a text version instead.
3. Click **Check & confirm** on each document. You see every paragraph and table row as the system read it:
   - Set the **section type** for each heading: Process description, Startup, Shutdown, Interlock/trip, Troubleshooting, Equipment specification, Other. Questions such as "describe the process" only search *Process description* sections.
   - Untick blocks that should not be searchable, such as cover pages.
   - Then click **Save & confirm for indexing**.
4. **Build search index**. The new index replaces the old one only if it finishes. If it fails, the old one keeps working.
5. **Who can read this**: give read access to each member who should get answers from this knowledge base.
6. **Agents → New agent**: name it, tick the knowledge bases, and keep the default models. The builder opens with a ready-made flow:
   `Input → Knowledge Retrieval → Prompt → LLM → Answer Review → Output`
7. In the builder you can:
   - click a node to change its settings (right panel);
   - drag new nodes from the top bar. **Condition** sends the answer down a *true* or *false* branch, for example "review verdict is pass";
   - connect ports of the same colour. An LLM draft can only reach an Output through **Answer Review**, and the builder will not let you connect it any other way;
   - use **Undo / Redo** (Ctrl+Z / Ctrl+Y), **Check**, and **Save version**. Every save is a new version, and older versions can be viewed and restored;
   - use **List & form view** for the same agent as forms and a connection table, without dragging (keyboard- and screen-reader-friendly);
   - use **Run test**. Each node lights up with its real status and time. Builders also see the hidden draft and the review findings.
8. **Published assistants → Publish an agent**. Publishing again with the same address keeps the previous version, so **Roll back** is one click. **New key** creates an API key for another plant system. The key is shown once.

## Engineer reviewers
1. **Review queue** lists answers the checks flagged, with the reasons (e.g. `equipment_value_binding`: "the value 5 bar belongs to 10-P-101A, not 10-C-101").
2. Open an item. You see the question, the retrieved sources, the model draft (the user never saw it) and the findings.
3. Choose one:
   - **Save correction & release**: write the right answer and cite sources as [n]. **Run built-in checks on my text** first to catch slips.
   - **Approve draft as is**: when the draft was actually right.
   - **Reject**: explain why in the note.
4. Your decision is permanent, carries your name, and appears in the decision history. Approving or correcting also *proposes* the Q&A for training. A different person must still approve it before it can be used.

## Training operators
1. **Examples**: review the proposed examples. The *preparation suggestions* flag duplicates, contradictions, possible personal data, missing citations and poor examples. Use **Include**, **Exclude** or **Edit / redact**. You cannot include an example that you approved yourself as an engineer.
2. **Dataset versions**: each source-document group goes wholly into *train*, *validation* or *held-out*, so test questions cannot leak into training. **Create version** freezes it with a SHA-256 fingerprint.
3. **Training & evaluation**: request a job (dataset version + registered base model + allow-listed recipe). **Another** operator approves it. Preflight then checks the GPU, memory, disk, model files, licence and dataset. If something is missing, the job shows **blocked** with the exact reason. While it runs you see the real loss curve. Cancel works at any time and checkpoints are kept.
4. **Evaluate on held-out set** compares the base model and the trained candidate on the same frozen questions. The scores are deterministic, with no AI judge.
5. **Deployment** (needs the *Model promotion* permission, and not the person who asked for the training): approve, and the model is imported into Ollama as `promoted:<name>`. Builders choose it in an LLM node, so nothing changes automatically. **Roll back** switches to the previous deployment. Requests already running keep their model.

Fine-tuning improves style, format and terminology. Plant facts that change (values, procedures) belong in the knowledge bases, where access rules and revisions apply.

## New in 0.3: public models, MCP tools, agents, teams and GraphRAG

**Your own API keys.** Open **Models, keys & MCP tools → Model connections → Add API key**. Choose the provider (OpenAI, Anthropic, Gemini or an OpenAI-compatible service), give the key a short name (e.g. `my-openai`), paste the key, then click **Test & list models**. The key is stored encrypted and is never shown again. Anthropic has no embedding models, so for semantic search, add an OpenAI or Gemini key as well.

**MCP tools.** Under **MCP servers & tools → Add MCP server**, enter the server's https URL and, if needed, an auth header such as `Authorization: Bearer …`. The tools appear **disabled**; switch on only the ones you trust. Each server has a classification limit, and content above it is never sent to that server.

**Knowledge graph (GraphRAG).**
- When you create a knowledge base, or later under its **Index settings**, choose the embedding connection and model, and a graph mode:
  - **Rules** finds equipment tags and headings at no cost;
  - **Model extraction** finds any entities and relations and writes topic summaries, which costs tokens.
- Build the index, then open **Knowledge graph** to explore the graph.
- In a Retrieval node or an agent, set **Knowledge graph** to:
  - **Local** for questions about specific items;
  - **Global** for big-picture questions;
  - **Both**.

**Agents.** In a project, **Agents → New agent** offers three templates: **Knowledge Q&A** (reviewed answers), **Tool-using agent** and **Simple chat**. In the builder, the **Agent** node has its own instructions, model, knowledge bases, MCP tools and a step limit. The **Agent Team** node is a leader that delegates to up to six members; each member can use a different provider, model, knowledge bases and tools. Test in the right-hand panel; every tool call and delegation is listed.

**Review.** Projects that require review must end with Answer Review → Output. In your own workspace on a public server, you may instead end with **Direct Output**. The answer is then shown without the checks, so treat it as unverified.

**NexAgent Lite** is the same idea without a server: it runs completely in your browser (see `lite/README.md`).
