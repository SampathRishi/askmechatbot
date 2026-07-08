"""
server.py — Phase 5: FastAPI backend that serves the chat UI and the RAG API.

Endpoints:
  GET  /health          -> {status, indexed_chunks, model}
  GET  /brand           -> brand tokens (data/brand/brand.json) for the UI theme
  GET  /brand/logo.*    -> the downloaded site logo
  POST /chat            -> {answer, citations, grounded}  (question + session_id)
  POST /reset           -> clears a session's conversation memory
  GET  /  (+ static)    -> the built React app (frontend/dist)

Input guards (also part of Phase 6 hardening):
  * question length capped at MAX_QUESTION_CHARS; empty/whitespace refused
  * basic per-session rate limit (RATE_LIMIT_PER_SESSION_PER_MIN)
Retrieved website content is passed to the LLM strictly as data (see rag.py),
which provides the prompt-injection resistance.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from rag import answer as rag_answer
from rag import conversation_store

app = FastAPI(title="Cameron County Website Assistant", version="1.0")

# CORS: permissive for local dev; the built UI is served same-origin in prod.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #

class ChatRequest(BaseModel):
    question: str = Field(..., description="the user's question")
    session_id: str = Field(..., min_length=1, max_length=128)


class Citation(BaseModel):
    n: int
    source_url: str
    page_title: str
    heading_path: str
    snippet: str


class ChatResponse(BaseModel):
    answer: str
    citations: List[Citation]
    grounded: bool


class ResetRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128)


# --------------------------------------------------------------------------- #
# Per-session rate limiter (in-process, sliding 60s window)
# --------------------------------------------------------------------------- #

_rate_hits: Dict[str, Deque[float]] = defaultdict(deque)


def _rate_limited(session_id: str) -> bool:
    now = time.monotonic()
    window = _rate_hits[session_id]
    while window and now - window[0] > 60.0:
        window.popleft()
    if len(window) >= config.RATE_LIMIT_PER_SESSION_PER_MIN:
        return True
    window.append(now)
    return False


# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health() -> JSONResponse:
    count = None
    try:
        from indexer import get_collection
        count = get_collection(create=False).count()
    except Exception:  # noqa: BLE001 - health should never 500 on a cold index
        count = None
    return JSONResponse({
        "status": "ok",
        "indexed_chunks": count,
        "model": config.ANTHROPIC_MODEL,
    })


@app.get("/brand")
def brand() -> JSONResponse:
    if config.BRAND_JSON.exists():
        data = json.loads(config.BRAND_JSON.read_text(encoding="utf-8"))
        # expose the logo via a served URL the frontend can use directly
        if data.get("logo"):
            data["logo_path"] = f"/brand/{data['logo']}"
        return JSONResponse(data)
    return JSONResponse({"fallbacks": ["all"]})


@app.get("/brand/{filename}")
def brand_asset(filename: str) -> FileResponse:
    # only serve files that actually live in the brand dir (no traversal)
    safe = (config.BRAND_DIR / filename).resolve()
    if not str(safe).startswith(str(config.BRAND_DIR.resolve())) or not safe.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(safe)


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    # ---- input guards ----
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    if len(question) > config.MAX_QUESTION_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Question too long (max {config.MAX_QUESTION_CHARS} characters).",
        )
    if _rate_limited(req.session_id):
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait a moment and try again.",
        )

    # ---- answer with per-session conversation memory ----
    history = conversation_store.history(req.session_id)
    try:
        result = rag_answer(question, conversation_history=history)
    except Exception as exc:  # noqa: BLE001 - surface a clean 502 to the UI
        raise HTTPException(status_code=502,
                            detail=f"The assistant is temporarily unavailable: {exc}")

    # record the turn (question + answer) for follow-up context
    conversation_store.append(req.session_id, "user", question)
    conversation_store.append(req.session_id, "assistant", result["answer"])

    return ChatResponse(**result)


@app.post("/reset")
def reset(req: ResetRequest) -> JSONResponse:
    conversation_store.clear(req.session_id)
    _rate_hits.pop(req.session_id, None)
    return JSONResponse({"status": "cleared"})


# --------------------------------------------------------------------------- #
# Demo site (offline county-homepage replica + embedded chat widget).
# Mounted at /demo so one server hosts both the demo page and the chatbot; the
# widget iframes "/" (this same origin), which serves the chat UI below.
# Must be mounted BEFORE the "/" catch-all so it isn't shadowed.
# --------------------------------------------------------------------------- #

_DEMO_SITE = config.PROJECT_ROOT / "demo-site"
if _DEMO_SITE.is_dir():
    app.mount("/demo", StaticFiles(directory=str(_DEMO_SITE), html=True), name="demo")


# --------------------------------------------------------------------------- #
# Static frontend (built React app). Mounted LAST so it doesn't shadow the API.
# --------------------------------------------------------------------------- #

_FRONTEND_DIST = config.PROJECT_ROOT / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="static")
else:
    @app.get("/")
    def _no_build() -> JSONResponse:
        return JSONResponse({
            "message": "Frontend not built yet. Run `npm install && npm run build` "
                       "in the frontend/ directory.",
        })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT)
