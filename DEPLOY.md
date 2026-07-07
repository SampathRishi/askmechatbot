# Deploying the Cameron County RAG chatbot

This app is **one FastAPI service** that serves both the chat API and the built
React UI (`server.py` mounts `frontend/dist` at `/`). It needs a host that can
run a **persistent Python server with real RAM and a disk** — because it loads a
local embedding model (PyTorch / `bge-small`) and reads a **~200 MB ChromaDB
vector index** from disk.

> **Why not Vercel?** Vercel runs short-lived serverless functions capped at
> 250 MB with an ephemeral filesystem. PyTorch alone is ~1 GB and the vector
> index can't live there. Use a server host (Render/Railway/Fly.io) instead.
> A blueprint for **Render** is included (`render.yaml`).

---

## Deploy to Render (recommended, ~10 min)

### 1. Prerequisites
- A [Render](https://render.com) account.
- Your `ANTHROPIC_API_KEY`.
- This repo pushed to GitHub (already done: `SampathRishi/askmechatbot`).

### 2. Create the service from the blueprint
1. Render dashboard → **New +** → **Blueprint**.
2. Connect the repo `SampathRishi/askmechatbot`. Render reads `render.yaml` and
   proposes one web service + a 1 GB disk mounted at `/var/data`.
3. Click **Apply**. The first build installs PyTorch etc. (a few minutes).

### 3. Set the API key
Service → **Environment** → confirm/add `ANTHROPIC_API_KEY = <your key>`
(the blueprint marks it `sync: false`, so you enter it in the dashboard, not git).

### 4. Get the vector index onto the disk
The `data/` folder is gitignored (too large for git), so the fresh server has an
empty index. Pick ONE:

**Option A — upload your existing local index (fast, no re-crawl).**
Render Standard supports SSH. From your project folder locally:
```powershell
# find your service's SSH address in the Render dashboard (Settings -> SSH)
scp -r data/chroma  <srv-id>@ssh.oregon.render.com:/var/data/
scp -r data/brand   <srv-id>@ssh.oregon.render.com:/var/data/
```
(You only need `data/chroma` and `data/brand` at runtime — not `raw/`/`processed/`.)

**Option B — rebuild on the server (slow, ~3 h crawl).**
Service → **Shell**, then:
```bash
python pipeline.py            # crawl -> clean -> index -> brand, writes to /var/data
```

After either, **Manual Deploy → Restart** so the server picks up the index.

### 5. Verify
- `https://<your-service>.onrender.com/health` → `{"status":"ok","indexed_chunks":11705,...}`
- Open the root URL → the branded chat UI. Ask a question.

---

## Using the embeddable widget on another site
The demo widget in [`demo-site/chatbot-widget.js`](demo-site/chatbot-widget.js)
iframes the assistant. Set `iframeUrl` to your deployed URL:
```js
iframeUrl: "https://<your-service>.onrender.com",
```
You can host `demo-site/` (or `frontend/`) as a **static site on Vercel** and
point it at the Render backend — Vercel is fine for the static front end.

---

## Cost & sizing notes
- The backend needs **≥ 2 GB RAM** (PyTorch + Chroma). Render **Standard**
  (~$25/mo) fits; the free/512 MB tiers OOM.
- The disk only needs ~200 MB (the Chroma index); 1 GB in the blueprint is ample.
- Rebuilding the frontend is **not** required on the host — `frontend/dist` is
  committed. Rebuild locally (`cd frontend && npm run build`) and commit when the
  UI changes.

## Fully-serverless alternative (more work)
To run on Vercel/serverless you'd refactor the heavy local pieces out:
- swap `bge-small` for an **embeddings API** (Voyage/OpenAI/Cohere) in
  `embeddings.py` (the `Embedder` interface is already pluggable), and
- move the vector store to a **hosted DB** (Chroma Cloud / Qdrant / Pinecone).

Then a slim Python function fits under Vercel's limits. This adds paid services
and code changes, so the single-server deploy above is simpler.
