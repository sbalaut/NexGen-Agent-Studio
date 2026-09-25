# Administrator guide

## Accounts and roles
- Roles can be combined per person: **Admin** (accounts, model connections, audit), **Builder** (knowledge bases, agents, publishing), **Reviewer** (engineer review), **TrainingOperator** (datasets, training, evaluation), **Viewer** (uses assistants). **Model promotion** is a separate permission.
- Project access is separate: *owner* (members, access, publishing), *editor* (build), *viewer* (use). Knowledge-base read/write is granted per member by a project owner.
- Changing roles or disabling a person signs out all their sessions immediately. The last active administrator and the last project owner cannot be removed.
- Sign-in protection: after 5 wrong passwords for one account (or 30 from one address) the wait grows 30 s → 60 s → … up to 15 min. There is no site-wide lock, so one person cannot lock everybody out.

## Models
- **Administration → Models**: default answer, review and embedding models, chosen from what `ollama list` shows. After changing the embedding model, rebuild every knowledge-base index.
- **Connections**: `local-ollama` exists by default and may receive Restricted content. Other connections (another GPU server, a vLLM endpoint) must be listed in `NEXAGENT_ALLOWED_MODEL_HOSTS`. Each has a *highest classification it may receive*, and anything above it is blocked **before** bytes are sent. External connections may never receive Restricted content. API keys for connections are encrypted with `data/connections.key`, which is kept outside the database.
- **System status** shows whether Ollama is reachable and flags configured models that are not pulled.

## Backups and restore
```bash
scripts/run.sh backup /path/to/backups      # online-consistent copy of DB + uploads + secret keys
```
The archive contains secrets, so store it like the server itself. Training folders (`data/training/`, checkpoints and adapters) are large and not included; copy them separately if needed.

Restore into an **empty** data directory:
```bash
scripts/run.sh stop
NEXAGENT_DATA_DIR=/new/empty/dir venv/bin/python -m nexagent.cli restore /path/to/nexagent-backup-XXXX.tar.gz   # run in backend/
```
Test a restore once before relying on it. The automated test suite performs one.

## Audit
Every permission change, upload, index activation, publication, review decision, dataset approval, training request/approval/start/finish, promotion and rollback is recorded in an **append-only** log (the database rejects edits and deletes). View and filter it under **Administration → Audit log**.

## Deleting documents
Deleting a document removes it from search and source viewing at once. Dataset versions that used it are marked **restricted**, and trained adapters built from those datasets are marked **restricted** so they cannot be evaluated or promoted. Deleting data does not remove what a trained model has already learned. Retire the artifact instead.

## Operations
- Logs: `data/logs/nexagent.log`. Every error response carries a request id you can search for.
- Background work (indexing, questions, training) is stored in the database. After a restart, unfinished work resumes. A training job whose runner died resumes from its last checkpoint and says so in the audit log.
- Single server: there is no high availability. If the server stops, the assistants stop until it is back.
- SQLite handles a plant team comfortably (dozens of concurrent users). For hundreds of simultaneous users, plan a move to PostgreSQL.
