# Container image for the Cameron County RAG chatbot (FastAPI backend + built
# React UI in one process). Works on any container host — Hugging Face Spaces
# (free CPU tier, ~16 GB RAM), Fly.io, Railway, Render, etc.
#
# HF Spaces convention: listen on port 7860.
FROM python:3.11-slim

# build-essential is needed by some wheels (e.g. chromadb deps) on slim images
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# install deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app code (respect .dockerignore so .venv/data/node_modules aren't copied)
COPY . .

# Pre-download the embedding model into the image so first request is fast and
# no network is needed at runtime. Safe to remove if you prefer a smaller image.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"

ENV PORT=7860
EXPOSE 7860

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-7860}"]
