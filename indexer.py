"""
indexer.py — Phase 3: embed chunks and store them in a persistent ChromaDB.

  * Reads data/processed/chunks.jsonl.
  * Embeds each chunk with the pluggable Embedder (config.EMBEDDING_MODEL),
    PREPENDING the heading path to the chunk text before embedding so
    section-specific questions retrieve the right passage. The raw chunk text
    (without the prepended path) is stored as the Chroma document so citations
    show the real content.
  * Stores vectors + full metadata + raw text in a persistent collection at
    data/chroma/, using cosine space.
  * Idempotent: the collection is rebuilt from scratch each run and chunk_id is
    the Chroma document ID, so re-running never creates duplicates.
  * search(query, k) returns chunks with cosine-similarity scores + metadata.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Dict, List

import chromadb

import config
from embeddings import get_embedder


# Common English stopwords to ignore when computing keyword overlap.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "at", "by",
    "is", "are", "was", "were", "be", "do", "does", "did", "how", "what",
    "when", "where", "who", "which", "why", "can", "could", "would", "should",
    "i", "you", "it", "its", "my", "me", "we", "they", "them", "this", "that",
    "with", "about", "there", "here", "have", "has", "will", "get", "much",
    "any", "some", "please", "tell", "need", "want", "county", "cameron",
}


def _query_terms(query: str) -> List[str]:
    words = re.findall(r"[a-z0-9]+", query.lower())
    return [w for w in words if len(w) >= 3 and w not in _STOPWORDS]


def _keyword_boost(terms: List[str], heading: str, text: str) -> float:
    """Additive boost in [0, KEYWORD_BOOST_WEIGHT] based on how many distinct
    query terms appear in the chunk (heading matches weighted double)."""
    if not terms:
        return 0.0
    heading_l = heading.lower()
    text_l = text.lower()
    hits = 0.0
    for t in set(terms):
        in_head = re.search(rf"\b{re.escape(t)}\b", heading_l) is not None
        in_text = re.search(rf"\b{re.escape(t)}\b", text_l) is not None
        if in_head:
            hits += 1.0            # heading match: full weight
        elif in_text:
            hits += 0.5            # body match: half weight
    frac = min(1.0, hits / len(set(terms)))
    return config.KEYWORD_BOOST_WEIGHT * frac


# --------------------------------------------------------------------------- #
# Chroma client / collection
# --------------------------------------------------------------------------- #

def _client() -> chromadb.ClientAPI:
    return chromadb.PersistentClient(path=str(config.CHROMA_DIR))


def get_collection(create: bool = False):
    """Return the collection; optionally (re)create it fresh for a clean index."""
    client = _client()
    if create:
        try:
            client.delete_collection(config.CHROMA_COLLECTION)
        except Exception:  # noqa: BLE001 - collection may not exist yet
            pass
        return client.create_collection(
            name=config.CHROMA_COLLECTION,
            metadata={"hnsw:space": "cosine"},  # so distance == 1 - cosine sim
        )
    return client.get_collection(config.CHROMA_COLLECTION)


def _embed_text(chunk: dict) -> str:
    """Text actually fed to the embedder: heading path + chunk body."""
    hp = (chunk.get("heading_path") or "").strip()
    body = chunk.get("text", "")
    return f"{hp}\n{body}" if hp else body


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #

def build_index() -> int:
    if not config.CHUNKS_JSONL.exists():
        raise FileNotFoundError(
            f"{config.CHUNKS_JSONL} not found — run processor.py first."
        )

    chunks: List[dict] = []
    with config.CHUNKS_JSONL.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))

    if not chunks:
        print("[index] no chunks to index.")
        return 0

    embedder = get_embedder()
    print(f"[index] embedder={embedder.name} dim={embedder.dimension} "
          f"chunks={len(chunks)}")

    collection = get_collection(create=True)

    batch = config.EMBEDDING_BATCH_SIZE
    total = 0
    for i in range(0, len(chunks), batch):
        part = chunks[i:i + batch]
        embed_inputs = [_embed_text(c) for c in part]
        vectors = embedder.embed_documents(embed_inputs)
        collection.upsert(
            ids=[c["chunk_id"] for c in part],
            embeddings=vectors,
            documents=[c["text"] for c in part],
            metadatas=[_metadata(c) for c in part],
        )
        total += len(part)
        print(f"[index] {total}/{len(chunks)} embedded", end="\r")

    print(f"\n[index] done. collection '{config.CHROMA_COLLECTION}' "
          f"count={collection.count()}")
    return total


def _metadata(c: dict) -> Dict:
    # Chroma metadata values must be scalar (str/int/float/bool).
    return {
        "source_url": c.get("source_url", ""),
        "page_title": c.get("page_title", ""),
        "heading_path": c.get("heading_path", ""),
        "chunk_index": int(c.get("chunk_index", 0)),
        "crawl_timestamp": c.get("crawl_timestamp", ""),
        "token_count": int(c.get("token_count", 0)),
        "is_pdf": bool(c.get("is_pdf", False)),
    }


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def search(query: str, k: int = config.RETRIEVAL_TOP_K) -> List[Dict]:
    """Return the top-k chunks for `query`, hybrid-ranked.

    Candidates come from two sources, merged and de-duplicated:
      1. a dense ANN pool (RETRIEVAL_CANDIDATE_POOL nearest neighbours), and
      2. a lexical branch — for each distinctive query term, the best chunks that
         literally CONTAIN that term (Chroma $contains), scored by the same query
         vector so their true cosine is preserved.

    The union is re-ranked by (cosine similarity + keyword-overlap boost) and
    truncated to k. Each result keeps its TRUE cosine similarity in `score` (so
    the RAG relevance gate stays calibrated) plus a `rerank_score` for ordering.

    The lexical branch matters because the dense ANN search is approximate: an
    exact-answer chunk (especially a small or newly-added one) can have the
    highest true cosine yet never appear in a shallow neighbour pool. Fetching it
    by keyword guarantees it is considered.
    """
    collection = get_collection(create=False)
    qvec = get_embedder().embed_query(query)
    pool = max(k, config.RETRIEVAL_CANDIDATE_POOL)
    terms = _query_terms(query)

    # merge candidates by chunk id so dense + lexical hits don't double-count
    merged: Dict[str, Dict] = {}

    def _absorb(res: dict) -> None:
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        for cid, doc, meta, dist in zip(ids, docs, metas, dists):
            if cid in merged:
                continue
            cosine = round(1.0 - float(dist), 4)
            merged[cid] = {"score": cosine, "text": doc, **meta}

    # 1. dense pool
    _absorb(collection.query(
        query_embeddings=[qvec],
        n_results=pool,
        include=["documents", "metadatas", "distances"],
    ))

    # 2. lexical branch: chunks that literally contain each distinctive term.
    # Chroma's $contains is case-SENSITIVE, but query terms are lowercased while
    # the answer may capitalize them (e.g. a heading-style chunk "District
    # Courts"). Try a few case variants per term so title/sentence-case chunks
    # are still rescued. Purely additive — it only widens the candidate set.
    seen_variants: set = set()
    for term in terms[:6]:
        for variant in (term, term.capitalize(), term.upper()):
            if variant in seen_variants:
                continue
            seen_variants.add(variant)
            try:
                _absorb(collection.query(
                    query_embeddings=[qvec],
                    n_results=config.LEXICAL_MATCHES_PER_TERM,
                    where_document={"$contains": variant},
                    include=["documents", "metadatas", "distances"],
                ))
            except Exception:  # noqa: BLE001 - empty match / hiccup: skip variant
                continue

    candidates: List[Dict] = []
    for c in merged.values():
        boost = _keyword_boost(terms, c.get("heading_path", ""), c["text"])
        c["rerank_score"] = round(c["score"] + boost, 4)
        candidates.append(c)

    candidates.sort(key=lambda c: c["rerank_score"], reverse=True)
    return candidates[:k]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    if len(sys.argv) > 1:
        # search mode:  python indexer.py "your question here"
        q = " ".join(sys.argv[1:])
        for i, r in enumerate(search(q), 1):
            print(f"\n[{i}] score={r['score']}  {r['source_url']}")
            print(f"    heading: {r['heading_path']}")
            print(f"    {r['text'][:200].replace(chr(10), ' ')}...")
    else:
        build_index()
