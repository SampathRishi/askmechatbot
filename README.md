# Cameron County — Website-Grounded RAG Chatbot

A production-quality retrieval-augmented chatbot that answers questions using
**only** content crawled from [cameroncountytx.gov](https://www.cameroncountytx.gov/).
Every answer is grounded in retrieved website content and carries citations; when
the site does not contain the answer, the bot says so instead of guessing.

> **Top quality bar:** no hallucinations. When in doubt, the bot returns
> *"The information you're asking about is not available on this website."*
> A correct refusal is a success; a confident-but-wrong answer is a failure.

---

## How it works (architecture)

```
  crawler.py ──► data/raw/pages.jsonl        (Phase 1: crawl HTML + PDFs)
       │
  processor.py ─► data/processed/chunks.jsonl (Phase 2: clean + heading-aware chunk)
       │
  indexer.py ──► data/chroma/                 (Phase 3: embed + ChromaDB)
       │
  rag.py ──────► answer(question, history)    (Phase 4: retrieve → gate → grounded LLM)
       │
  server.py ───► FastAPI: /chat /health /brand + serves the React UI (Phase 5)
       ▲
  frontend/ ───► brand-mirrored React chat UI (Phase 5)
  brand_extractor.py ─► data/brand/brand.json (Phase 5: brand tokens from homepage)
  pipeline.py ─► runs crawl → clean → index → brand end-to-end (Phase 6)
```

- **Crawling:** `httpx` + `BeautifulSoup4`, seeded from the WordPress/Yoast sitemap,
  with `pypdf` for PDF text. Playwright is only used as a JS-render fallback (this
  site never needed it).
- **Cleaning/chunking:** `trafilatura` main-content extraction, heading-aware
  chunking (≤480 tokens, 100-token overlap, sentence-boundary splits).
- **Embeddings:** `sentence-transformers` `BAAI/bge-small-en-v1.5` (local, free),
  behind a swappable `Embedder` interface (`embeddings.py`).
- **Vector store:** ChromaDB (persistent, cosine) with hybrid dense + keyword
  re-ranking.
- **LLM:** Anthropic `claude-sonnet-4-6` (key from `ANTHROPIC_API_KEY`), with a
  fast `claude-haiku-4-5` follow-up rewriter.
- **Frontend:** single-page React (Vite), themed from the extracted brand tokens,
  served by FastAPI.

---

## Prerequisites

- **Python 3.11+**
- **Node.js 18+** (only needed to build the frontend)
- An **Anthropic API key** (for the answer engine)

---

## 1. Install

```powershell
# from the project root
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> On Windows, PyTorch (a `sentence-transformers` dependency) needs the Microsoft
> Visual C++ Redistributable. If you see `WinError 1114` importing torch, install
> it: `winget install Microsoft.VCRedist.2015+.x64`.

Set your API key — create a file named **`.env`** in the project root:

```
ANTHROPIC_API_KEY=sk-ant-...your key...
```

(Copy `.env.example` to `.env`. The `.env` file is gitignored.)

---

## 2. Build the data (crawl → clean → index → brand)

Run the whole pipeline with one command:

```powershell
.\.venv\Scripts\python.exe pipeline.py
```

This runs, in order: **crawl → clean/chunk → embed/index → brand extraction**.
The first crawl takes a while (polite 2 req/s; ~1,000 pages + 500 PDFs).

Run individual stages if you prefer:

```powershell
.\.venv\Scripts\python.exe crawler.py          # Phase 1  -> data/raw/pages.jsonl
.\.venv\Scripts\python.exe processor.py        # Phase 2  -> data/processed/chunks.jsonl
.\.venv\Scripts\python.exe indexer.py          # Phase 3  -> data/chroma/
.\.venv\Scripts\python.exe brand_extractor.py  # Phase 5  -> data/brand/brand.json
```

Reuse an existing crawl and only re-process/re-index:

```powershell
.\.venv\Scripts\python.exe pipeline.py --skip-crawl
# or a single stage:
.\.venv\Scripts\python.exe pipeline.py --only index
```

Optional — enable the JS-render fallback (rarely needed for this site):

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

---

## 3. Build the frontend

```powershell
cd frontend
npm install
npm run build          # outputs frontend/dist, which FastAPI serves
cd ..
```

---

## 4. Run the backend (serves the UI + API)

```powershell
.\.venv\Scripts\python.exe server.py
```

Open **http://127.0.0.1:8000** — the branded chat UI is served by FastAPI.

API endpoints:

| Method | Path            | Purpose                                            |
| ------ | --------------- | -------------------------------------------------- |
| POST   | `/chat`         | `{question, session_id}` → `{answer, citations, grounded}` |
| GET    | `/health`       | `{status, indexed_chunks, model}`                  |
| GET    | `/brand`        | brand tokens for the UI theme                      |
| POST   | `/reset`        | clear a session's conversation memory              |

Quick API test:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/chat -Method Post -ContentType application/json `
  -Body (@{question="What are the animal shelter adoption hours?"; session_id="test"} | ConvertTo-Json)
```

---

## Frontend dev mode (optional, hot reload)

Run the backend (`python server.py`) and, in a second terminal:

```powershell
cd frontend
npm run dev            # http://localhost:5173, proxies /chat etc. to :8000
```

---

## Re-crawling when the site updates

The whole dataset is regenerated from scratch, so re-crawling is safe and
idempotent (the vector index is rebuilt, not appended):

```powershell
.\.venv\Scripts\python.exe pipeline.py          # full refresh
# then rebuild the frontend only if brand tokens changed:
cd frontend; npm run build; cd ..
```

To capture more PDFs on a refresh, raise the cap first (see config below), e.g.
`MAX_PDFS=1500` in `.env`, then re-run the pipeline.

---

## Configuration (`config.py`)

Every tunable lives in `config.py` and can be overridden via `.env` or the
environment. The most useful:

| Setting                    | Default                      | Meaning                                   |
| -------------------------- | ---------------------------- | ----------------------------------------- |
| `BASE_URL`                 | `https://www.cameroncountytx.gov/` | site to crawl                       |
| `ALLOWLIST_DOMAINS`        | `cameroncountytx.gov`        | domains allowed in scope (comma-sep)      |
| `MAX_PAGES`                | `2000`                       | HTML page cap for a crawl                 |
| `MAX_PDFS`                 | `500`                        | PDF extraction cap                        |
| `MAX_PDF_SIZE_MB`          | `10`                         | skip PDFs larger than this                |
| `REQUESTS_PER_SECOND`      | `2.0`                        | crawl rate limit                          |
| `CHUNK_MAX_TOKENS`         | `480`                        | max chunk size (fits bge-small's 512)     |
| `CHUNK_OVERLAP_TOKENS`     | `100`                        | overlap between split chunks              |
| `EMBEDDING_MODEL`          | `BAAI/bge-small-en-v1.5`     | swap for a bigger/API model               |
| `RETRIEVAL_TOP_K`          | `8`                          | chunks sent to the LLM                    |
| `RETRIEVAL_CANDIDATE_POOL` | `40`                         | dense pool re-ranked before top-k         |
| `KEYWORD_BOOST_WEIGHT`     | `0.20`                       | hybrid keyword re-rank strength           |
| `SIMILARITY_THRESHOLD`     | `0.35`                       | relevance gate (below → not-available)    |
| `ANTHROPIC_MODEL`          | `claude-sonnet-4-6`          | answer model                              |
| `MAX_QUESTION_CHARS`       | `1000`                       | input guard                               |
| `RATE_LIMIT_PER_SESSION_PER_MIN` | `20`                   | per-session rate limit                    |

### Swapping the embedding model

The embedding layer is behind the `Embedder` interface in `embeddings.py`. To use
an API-based model later, implement the three methods (`embed_documents`,
`embed_query`, plus `name`/`dimension`) and register it in `get_embedder()`, then
set `EMBEDDING_PROVIDER`. Re-run `indexer.py` to rebuild the index with the new
model.

---

## Safety & grounding notes

- **Relevance gate:** if the best cosine similarity is below
  `SIMILARITY_THRESHOLD`, the LLM is never called — the not-available message is
  returned directly.
- **Strict grounding prompt:** the model must answer only from the provided
  passages, cite every claim with `[n]`, and return the not-available message
  when the answer isn't present. Answers with zero citations are never shown.
- **Prompt-injection resistance:** retrieved website content is passed to the
  model as data inside `<passage>` XML tags, and the system prompt instructs the
  model to treat it strictly as reference material, never as instructions.
- **Input guards:** empty questions are refused, questions are capped at
  `MAX_QUESTION_CHARS`, and each session is rate-limited.

---

## Project layout

```
config.py            all tunables (env-overridable)
crawler.py           Phase 1 — crawl HTML + PDFs
processor.py         Phase 2 — clean + heading-aware chunk
text_utils.py        tokenizer + sentence utilities
embeddings.py        swappable Embedder interface
indexer.py           Phase 3 — embed + ChromaDB + hybrid search
rag.py               Phase 4 — grounded answer engine
brand_extractor.py   Phase 5 — brand tokens from the homepage
server.py            Phase 5 — FastAPI backend + static UI
pipeline.py          Phase 6 — crawl→clean→index→brand in one command
smoke_test.py        manual QA of the four answer scenarios
screenshot_ui.py     Playwright UI screenshot helper
frontend/            React (Vite) chat UI
data/                generated artifacts (gitignored)
```

---

## Known limitations

- **PDF coverage is capped** at 500 (the site hosts 6,000+ PDFs — mostly deep
  agenda/minutes/budget archives). Raise `MAX_PDFS` for fuller coverage.
- **Scanned/image-only PDFs** yield no text (no OCR in this version).
- **Affiliated domains are not crawled** (tax office, venues, etc. live on
  separate sites); every external domain seen is logged in
  `data/raw/crawl_report.json` so you can decide whether to add any to
  `ALLOWLIST_DOMAINS`.
- **OneDrive:** this project sits under a OneDrive-synced folder. The `data/`
  directory (crawl output + 244 MB vector index) will sync unless excluded;
  consider pausing OneDrive during large crawls.
