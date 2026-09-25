# Publishing NexAgent on GitHub and deploying it for public use

This guide covers three steps:

1. Put the code on GitHub.
2. Publish **NexAgent Lite** on GitHub Pages. It is free, has no server, and every visitor uses their own API key.
3. Deploy the **server edition** from the same repository to a cloud host, with accounts, sign-up and shared agents.

GitHub Pages can only host static files, so the server edition always needs a separate host. The server edition's image is built by the `Dockerfile` in this repository.

---

## 1. Put the code on GitHub

You need a GitHub account and `git` on your computer.

1. On github.com, click **New repository**. Name it, e.g. `nexagent-studio`. Choose **Public** or **Private**. Do **not** add a README, licence or .gitignore; this folder already has them.
2. In a terminal inside this folder (the one that contains `README.md`), run:

```bash
git init -b main
git add .
git commit -m "NexAgent Studio 0.3"
git remote add origin https://github.com/<your-user>/nexagent-studio.git
git push -u origin main
```

If you use the GitHub CLI, `gh repo create nexagent-studio --public --source . --push` does the same in one step.

Things to know:

- `.gitignore` keeps `data/`, `.env`, virtual environments, `node_modules` and build output **out of Git**. Never commit `.env`, `data/` or any API key.
- The server UI is committed pre-built in `backend/nexagent/static/`, so servers do not need Node.js.
- Choose a licence before others reuse the code. Add a `LICENSE` file, e.g. MIT or Apache-2.0; none is included.
- On every push, **Actions → CI** runs the backend tests, both UI builds, the Lite tests and a Docker build.

## 2. NexAgent Lite on GitHub Pages

1. In the repository, go to **Settings → Pages → Build and deployment → Source** and choose **GitHub Actions**.
2. Go to **Actions → Deploy NexAgent Lite to GitHub Pages → Run workflow**. After that, it runs by itself whenever `lite/` changes.
3. After about a minute the site is live at `https://<your-user>.github.io/nexagent-studio/`. The URL is also shown in the workflow run.

What visitors get:

- The whole app runs in their browser.
- Documents, knowledge graphs and agents are stored in that browser (IndexedDB).
- API keys stay in session storage (or local storage, if the visitor ticks "Remember keys").
- Model calls go straight from the browser to OpenAI, Anthropic, Gemini or the compatible service. Nothing passes through your GitHub account.
- Remote MCP servers work only if they allow browser access (CORS). See [lite/README.md](../lite/README.md).

## 3. Server edition in the cloud

Every host needs the same few settings:

| Variable | Value |
|---|---|
| `NEXAGENT_ORIGIN` | **Required.** The exact public https address, e.g. `https://nexagent.onrender.com` (no trailing slash). Logins fail with an "origin" error if it is wrong. |
| `NEXAGENT_PUBLIC_MODE` | `1`, the image default: open sign-up, bring-your-own keys, private workspaces |
| `NEXAGENT_DATA_DIR` | `/data`, the image default. Mount a **persistent disk** here. |
| `NEXAGENT_TRUSTED_PROXY` | `1`, the image default, because cloud hosts put a proxy in front |
| `NEXAGENT_BOOTSTRAP_ADMIN` / `NEXAGENT_BOOTSTRAP_ADMIN_PASSWORD` | Optional first administrator. It is created at start-up if none exists. **Delete the password variable after the first start.** |
| `NEXAGENT_ALLOW_SIGNUP` | `0` to close sign-up (the admin then creates accounts) |
| `NEXAGENT_QUOTA_RUNS_PER_HOUR`, `…_DOCS_PER_USER`, `…_UPLOAD_MB_PER_USER`, `NEXAGENT_SIGNUPS_PER_IP_PER_DAY` | Per-user limits (defaults 60 / 100 / 500 / 5) |

> **Persistent storage matters.** The database, uploaded files and the key that encrypts users' API keys live in `/data`. Without a persistent disk, **every restart or redeploy deletes all accounts and data**. Free tiers usually have no disk, so they are fine for a demo but not for real users. Back up `/data` regularly. `python -m nexagent.cli backup /data/backups` creates a consistent copy; it contains secrets, so store it securely.

### Render (uses `render.yaml`)
1. On render.com, choose **New → Blueprint** and pick your GitHub repository. Render reads `render.yaml`: a Docker web service with a 5 GB disk at `/data`. The disk requires a paid instance type.
2. When asked, enter `NEXAGENT_ORIGIN`. You only know the URL after the service is created, so enter a placeholder first. Then set it to `https://<service-name>.onrender.com` under **Environment** and redeploy.
3. Optionally set `NEXAGENT_BOOTSTRAP_ADMIN` and `NEXAGENT_BOOTSTRAP_ADMIN_PASSWORD`. Remove the password once you have logged in.
4. Every push to `main` redeploys automatically.

### Railway (uses `railway.json`)
1. **New Project → Deploy from GitHub repo**. Railway builds the `Dockerfile`.
2. On the service, open **Settings → Networking → Generate Domain**. Add a **Volume** mounted at `/data`.
3. Under **Variables**, set `NEXAGENT_ORIGIN=https://<generated-domain>` and optionally the bootstrap admin variables. Railway passes `PORT`, and the image uses it.

### Hugging Face Spaces (Docker Space)
1. Create a Space and choose **Docker → Blank**.
2. Push this repository to the Space's git remote. Put this block at the **top of the Space's README.md**; Spaces reads its settings from it:
   ```yaml
   ---
   title: NexAgent Studio
   sdk: docker
   app_port: 8600
   ---
   ```
3. Under **Settings → Variables and secrets**, set `NEXAGENT_ORIGIN=https://<user>-<space>.hf.space`. Put the admin password under *Secrets*.
4. Enable **Persistent storage** (paid). It is mounted at `/data`. Without it, data is lost whenever the Space restarts.
5. Open the app at the `*.hf.space` address directly. NexAgent forbids being framed, for click-jacking protection, so the embedded view on huggingface.co stays blank.

### Any Linux VM with Docker
```bash
docker build -t nexagent-studio .
docker run -d --name nexagent --restart unless-stopped -p 127.0.0.1:8600:8600 \
  -v nexagent-data:/data -e NEXAGENT_ORIGIN=https://agents.example.com nexagent-studio
```
Put a TLS reverse proxy in front. For example, Caddy with the one-line Caddyfile `agents.example.com { reverse_proxy 127.0.0.1:8600 }` obtains certificates automatically. For a server without Docker, use `scripts/install.sh` as in [INSTALL.md](INSTALL.md), set `NEXAGENT_PUBLIC_MODE=1`, and use the same reverse proxy.

## 4. After the first deployment

1. Open the address, create an account (or sign in as the bootstrap admin), and add your own API key under **Models, keys & MCP tools**. Use **Test & list models** to check the key.
2. In **My workspace**, create a knowledge base, upload a document and build the index. Then create an agent with the guided setup and test it.
3. As admin, review **Users, models & audit**: accounts, shared connections (optional; they are paid by you) and the audit log.
4. Publish short terms of use and a privacy notice. Users upload documents and store API keys on your server. Technically, the server operator could use those keys, and the notice in the app says so.
5. Tell users to set **spending limits** with their model providers.

## Updating

Push to `main`. Render, Railway and the Pages workflow redeploy automatically. Database migrations run at start-up, only move forward, and keep existing data. Back up `/data` before large updates.
