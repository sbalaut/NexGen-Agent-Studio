# Installing NexAgent Studio on a JupyterHub / GPU server

No Docker and no root access are needed. Everything lives in one folder, and the data lives in `data/` inside it unless you set `NEXAGENT_DATA_DIR`.

## 1. Prerequisites

| Need | Check with | Notes |
|---|---|---|
| Linux, Python 3.10+ with SQLite FTS5 | `scripts/install.sh` checks both | Conda/uv Pythons have FTS5 |
| Ollama running locally | `curl http://127.0.0.1:11434/api/tags` | Install per IT policy; `ollama serve` |
| Models pulled | `ollama list` | Defaults match the DHDT chatbot: `qwen3-next:80b-a3b-instruct-q4_K_M` (answers + review) and `qwen3-embedding:8b` (search). Any Ollama model works; change in `.env` or Administration → Models |
| ~2 GB disk + your documents | | Training needs more (see §6) |

## 2. Install

```bash
cd ~/nexagent-studio
scripts/install.sh            # online (PyPI)
# or, on a server without internet:
#   on a connected machine with the same OS + Python:  scripts/build_wheelhouse.sh
#   copy the folder including wheelhouse/, then:        scripts/install.sh --offline
```

This creates `venv/`, copies `.env.example` to `.env`, and creates the database. Then:

```bash
nano .env                      # see §3
scripts/run.sh create-admin    # asks for username and password (min. 12 characters)
scripts/run.sh start           # logs: data/logs/nexagent.log
scripts/run.sh check           # shows configuration and whether Ollama/models are reachable
```

To keep it running after logout, use `scripts/nexagent.service` (a systemd *user* unit; instructions inside) or ask IT to run `scripts/run.sh start` from a startup job.

## 3. How users reach it: pick one

The app listens on `127.0.0.1:8600` by default. It has its own sign-in, so users do not need JupyterHub accounts to use it.

**A. As a JupyterHub service (recommended when everyone already uses the hub).** Ask the hub administrator to add this to `jupyterhub_config.py`:
```python
c.JupyterHub.services = [{"name": "nexagent", "url": "http://127.0.0.1:8600"}]
```
Then in `.env`:
```
NEXAGENT_BASE_PATH=/services/nexagent
NEXAGENT_ORIGIN=https://<your-hub-host>          # exactly what users see in the address bar, no path
```
Users open `https://<your-hub-host>/services/nexagent/`.

**B. Direct HTTPS on the server.** Get an internal certificate from IT, then:
```
NEXAGENT_HOST=0.0.0.0
NEXAGENT_TLS_CERT=/path/server.crt
NEXAGENT_TLS_KEY=/path/server.key
NEXAGENT_ORIGIN=https://<server-name>:8600
```
Open the port only to the plant network (firewall rule by IT).

**C. Just you, for a pilot.** Keep the defaults and use an SSH tunnel: `ssh -L 8600:127.0.0.1:8600 you@server`, then open http://127.0.0.1:8600.

Plain `http://` on a non-loopback address is refused unless `NEXAGENT_ALLOW_HTTP=1` is set. Only set it on an isolated network you have approved, because passwords would travel unencrypted.

## 4. First-time setup in the browser (about 15 minutes)

1. **Administration → Models**: pick the default answer, review and embedding models from the list of installed Ollama models.
2. **Administration → Users & roles**: create accounts. Roles can be combined: Builder, Reviewer, TrainingOperator, Viewer, Admin, plus the separate *Model promotion* permission.
3. **Projects → New project**. Then **Members**: add people. Then **Knowledge bases → New** → upload manuals → **Check & confirm** each preview → **Build search index** → **Who can read this**.
4. **Agents → New agent** (guided setup) → the builder opens → **Run test** → **Published assistants → Publish**.

## 4a. Running with the DHDT models on one L40S (46 GB)

- **Same model for answers and review.** A second large model would force Ollama to swap models in and out of VRAM on every question. Reusing qwen3-next avoids that.
- **Models stay loaded.** `NEXAGENT_KEEP_ALIVE=30m` keeps the 80B model and the 8B embedding model resident between questions. If VRAM is too tight for both, ask the Ollama administrator to set `OLLAMA_MAX_LOADED_MODELS=2`; the embedding model can also run on CPU.
- **Full context.** `NEXAGENT_NUM_CTX=16384` is sent with every request. Ollama's own default is only a few thousand tokens and would silently cut off the sources.
- **Time per question.** One question means one embedding call, one draft, one review and sometimes a repair plus a second review. `NEXAGENT_LLM_TIMEOUT_S=600` allows for partial CPU offload. If answers are too slow, untick "Also ask a review model" in an agent's Answer Review node. The built-in number, unit and equipment checks still run, but drafts that pass them are then released without the model's second opinion.
- **Qwen3 specifics.** Any `<think>…</think>` text is removed before anything is checked or shown. Search queries get the instruction prefix that Qwen3-Embedding expects.
- **Documents only, headings allowed** (as in the DHDT bot). The default prompt allows only source facts, each cited, and asks for a `### Heading` per paragraph. Headings are not treated as facts, but an equipment tag in a heading must exist in the sources.
- **Changing the embedding model** (for example from an older index) needs **Build search index** again for every knowledge base. The existing DHDT FAISS files are not reused; upload the same DHDT documents instead.
- **Training:** fine-tuning the 80B model itself is not feasible on one L40S. Use a smaller base model (1.5–8B) for style/terminology adapters, or skip training.

## 5. Checking answer quality with your models

Index `samples/fictional-cedar-unit-manual-rev-b.md` in a test project, publish an agent over it, give an evaluator account read access, then run:
```bash
venv/bin/python evaluations/run_golden.py --url <NEXAGENT_ORIGIN><BASE_PATH> --user evaluator --assistant <assistant-id>
```
The assistant id is in the browser address bar when you open it under Assistants. The report shows the useful-answer rate, the false-answer rate on unanswerable questions, and the Hindi/Hinglish and adversarial groups.

## 6. Optional: fine-tuning environment

Training runs in a **separate** Python environment as a **separate process**. The web app never imports torch.

```bash
python3 -m venv ~/nexagent-train
~/nexagent-train/bin/pip install torch --index-url https://download.pytorch.org/whl/cu121   # match your CUDA driver
~/nexagent-train/bin/pip install -r requirements-train.txt
~/nexagent-train/bin/pip install bitsandbytes          # only for the QLoRA recipe
```
In `.env`:
```
NEXAGENT_TRAIN_PYTHON=/home/<you>/nexagent-train/bin/python
NEXAGENT_MODELS_DIR=/data/models/approved              # base models are copied here by an admin; nothing is downloaded
NEXAGENT_TRAIN_GPU=1                                   # reserve a GPU so live assistants are not slowed down
```
Copy a Hugging Face model folder (config.json, tokenizer files, *.safetensors, LICENSE) into the models directory. Then register it under **Administration → Models → Base models**.

**Deploying a trained model** merges the adapter and runs `ollama create`, so the `ollama` command must be available to the app user (`NEXAGENT_OLLAMA_CLI`). Ollama can import safetensors for common architectures (Llama, Mistral, Gemma, Qwen2 and others). If the import fails, the deployment shows the exact error and nothing is switched.

### Hardware guide (estimates only; the preflight check measures the real values)
| Base model | LoRA (bf16) | QLoRA (4-bit) |
|---|---|---|
| 1.5 B | ~8 GB VRAM | ~5 GB |
| 7–8 B | ~24 GB | ~10–12 GB |
| 14 B | ~40 GB | ~18 GB |

## 7. Upgrading

Stop the app, back up (`scripts/run.sh backup`), replace the code folder but keep `data/` and `.env`, then run `scripts/install.sh` and `scripts/run.sh start`. Database migrations run automatically and only move forward.
