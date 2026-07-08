# Deploying the Cameron County RAG chatbot

This app is **one FastAPI service** that serves everything together:

| Path        | Serves                                                            |
| ----------- | ---------------------------------------------------------------- |
| `/`         | the built React chat UI (`frontend/dist`)                         |
| `/demo/`    | the offline county-homepage replica with the embedded chat widget |
| `/chat`, `/health`, `/brand` | the RAG API                                     |

`server.py` mounts `demo-site/` at `/demo` (before the `/` catch-all) so the demo
page and the chatbot are hosted by the **same server on one origin** — the widget
in the demo iframes `/`, which is the chat UI. Deploying the service therefore
publishes both at `https://<your-service>/` and `https://<your-service>/demo/`.

It needs a host that can
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
- Open `.../demo/` → the county homepage replica with the chat launcher
  (bottom-right); it iframes the same-origin chat UI.

---

## Using the embeddable widget on another site
The demo widget in [`demo-site/chatbot-widget.js`](demo-site/chatbot-widget.js)
iframes the assistant. Its default `iframeUrl` is `"/"` (same origin), which is
correct when the widget is served by this app at `/demo/`.

To embed the chatbot on a **different** domain (a real customer site), copy the
one-line embed and set `iframeUrl` to the **full** deployed URL so `/` doesn't
resolve to the customer's own origin:
```js
iframeUrl: "https://<your-service>.onrender.com",
```
```html
<script src="https://<your-service>.onrender.com/demo/chatbot-widget.js"></script>
```
You can also host `demo-site/` (or `frontend/`) as a **static site on Vercel**
and point it at the Render backend — Vercel is fine for the static front end.

---

## Cost & sizing notes
- The backend needs **≥ 2 GB RAM** (PyTorch + Chroma). Render **Standard**
  (~$25/mo) fits; the free/512 MB tiers OOM.
- The disk only needs ~200 MB (the Chroma index); 1 GB in the blueprint is ample.
- Rebuilding the frontend is **not** required on the host — `frontend/dist` is
  committed. Rebuild locally (`cd frontend && npm run build`) and commit when the
  UI changes.

## Free hosting: Hugging Face Spaces (~16 GB RAM, no code changes)

Normal free tiers (Render free, Fly free) give only ~512 MB RAM and **OOM** on
PyTorch. **Hugging Face Spaces' free CPU tier gives ~16 GB RAM**, runs Docker,
and is the realistic free option. A `Dockerfile` + `.dockerignore` are included.

1. Create a **Docker Space**: https://huggingface.co/new-space → SDK = **Docker**.
2. Push this project into the Space's git repo. The Space needs the **vector
   index** too (gitignored here), so include `data/chroma` and `data/brand`:
   ```bash
   git clone https://huggingface.co/spaces/<you>/askmechatbot hf-space
   cd hf-space
   # copy the app + the runtime index from your project
   cp -r <project>/* .
   git lfs install
   git lfs track "data/chroma/**"        # index files are large -> use LFS
   cp -r <project>/data/chroma  data/chroma
   cp -r <project>/data/brand   data/brand
   git add -A && git commit -m "Deploy chatbot to HF Space" && git push
   ```
3. In the Space: **Settings → Variables and secrets → New secret**
   `ANTHROPIC_API_KEY = <your key>`.
4. Add this YAML front matter to the top of the Space's `README.md` so it builds
   as a Docker app on port 7860:
   ```yaml
   ---
   title: Ask Me Chatbot
   emoji: 💬
   colorFrom: blue
   colorTo: indigo
   sdk: docker
   app_port: 7860
   ---
   ```
5. The Space builds and serves at `https://<you>-askmechatbot.hf.space`.

Free-tier caveats: the Space is **public** (fine — this is public gov data;
your API key stays a secret), it **sleeps after inactivity** and cold-starts in
~10–20 s, and there's no guaranteed persistent disk (the index rides in the
repo via LFS, which is what the steps above do).

> **Want it free with a smaller footprint?** Do the refactor below (API
> embeddings + hosted vector DB) and the app fits a 512 MB free tier without
> PyTorch — but that's code changes + external services.

## Fully-serverless alternative (more work)
To run on Vercel/serverless you'd refactor the heavy local pieces out:
- swap `bge-small` for an **embeddings API** (Voyage/OpenAI/Cohere) in
  `embeddings.py` (the `Embedder` interface is already pluggable), and
- move the vector store to a **hosted DB** (Chroma Cloud / Qdrant / Pinecone).

Then a slim Python function fits under Vercel's limits. This adds paid services
and code changes, so the single-server deploy above is simpler.
