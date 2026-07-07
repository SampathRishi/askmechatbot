"""
add_homepage_quicklinks.py — index the homepage quick-link grids that the
crawler missed.

The county homepage renders its "Popular Services" and "Portals" grids with
JavaScript (TheGem/WPBakery button widgets), so the static HTML the crawler
fetched did not contain them — they were never indexed, and the assistant could
not answer "what are the popular services?" or "what are the county portals?".

This script reads the fully-rendered "Save As Complete Webpage" export we already
have on disk, extracts those two link grids (label + destination URL), builds two
clean chunks, appends them to data/processed/chunks.jsonl, and upserts them into
the existing ChromaDB collection (no full re-embed of the corpus).

Idempotent: re-running replaces the same two chunk_ids rather than duplicating.

    python add_homepage_quicklinks.py            # dry-run: print what it found
    python add_homepage_quicklinks.py --commit   # write chunks + update the index
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from bs4 import BeautifulSoup

import config

ROOT = Path(__file__).resolve().parent
SAVED_HOME = ROOT / "Cameron County Homepage - Cameron County.html"
HOME_URL = "https://www.cameroncountytx.gov/"
HOME_TITLE = "Cameron County Homepage - Cameron County"


def _clean(text: str) -> str:
    return " ".join(text.split())


def _links_in(scope) -> list[tuple[str, str]]:
    """Return [(label, href)] for the gem-button anchors under `scope`, deduped
    on label, skipping empties."""
    out, seen = [], set()
    for a in scope.select("a.gem-button"):
        label = _clean(a.get_text(" ", strip=True))
        href = (a.get("href") or "").strip()
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        out.append((label, href))
    return out


def extract_grids(html: str) -> dict[str, list[tuple[str, str]]]:
    soup = BeautifulSoup(html, "html.parser")

    # Popular Services: the dedicated #buttonsections row
    popular_scope = soup.select_one("#buttonsections")
    popular = _links_in(popular_scope) if popular_scope else []

    # Portals: the row that holds the "Cameron County Portals" banner image
    portals: list[tuple[str, str]] = []
    banner = soup.find("img", alt=lambda v: v and "portals" in v.lower())
    if banner:
        row = banner
        for _ in range(8):  # climb to the enclosing vc_row
            row = row.parent
            if row is None:
                break
            cls = " ".join(row.get("class", []))
            if "vc_row" in cls and row.select("a.gem-button"):
                portals = _links_in(row)
                break
    return {"popular": popular, "portals": portals}


def _chunk(chunk_id: str, heading: str, intro: str,
           links: list[tuple[str, str]], source_url: str) -> dict:
    body_lines = [intro, ""]
    for label, href in links:
        body_lines.append(f"- {label}: {href}" if href else f"- {label}")
    text = "\n".join(body_lines)
    return {
        "chunk_id": chunk_id,
        "source_url": source_url,
        "page_title": HOME_TITLE,
        "heading_path": heading,
        "text": text,
        "chunk_index": 0,
        "crawl_timestamp": "",
        "token_count": len(text.split()),
        "is_pdf": False,
    }


def build_chunks(grids: dict) -> list[dict]:
    chunks = []
    if grids["popular"]:
        chunks.append(_chunk(
            "homepage-quicklinks-popular-services",
            "Popular Services",
            "Popular Services quick links featured on the Cameron County homepage:",
            grids["popular"], HOME_URL,
        ))
    if grids["portals"]:
        chunks.append(_chunk(
            "homepage-quicklinks-portals",
            "Cameron County Portals",
            "Cameron County online portals linked from the county homepage:",
            grids["portals"], HOME_URL,
        ))
    return chunks


def _rewrite_jsonl(new_chunks: list[dict]) -> None:
    ids = {c["chunk_id"] for c in new_chunks}
    path = config.CHUNKS_JSONL
    kept = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            if json.loads(line).get("chunk_id") in ids:
                continue  # drop old copies so re-runs don't duplicate
            kept.append(line.rstrip("\n"))
    with path.open("w", encoding="utf-8") as fh:
        for line in kept:
            fh.write(line + "\n")
        for c in new_chunks:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")


def _upsert(new_chunks: list[dict]) -> None:
    from embeddings import get_embedder
    from indexer import get_collection, _embed_text

    embedder = get_embedder()
    coll = get_collection(create=False)
    coll.upsert(
        ids=[c["chunk_id"] for c in new_chunks],
        embeddings=embedder.embed_documents([_embed_text(c) for c in new_chunks]),
        documents=[c["text"] for c in new_chunks],
        metadatas=[{
            "source_url": c["source_url"],
            "page_title": c["page_title"],
            "heading_path": c["heading_path"],
            "chunk_index": c["chunk_index"],
            "crawl_timestamp": c["crawl_timestamp"],
            "token_count": c["token_count"],
            "is_pdf": c["is_pdf"],
        } for c in new_chunks],
    )
    print(f"[quicklinks] upserted {len(new_chunks)} chunks; "
          f"collection count now {coll.count()}")


def main() -> None:
    commit = "--commit" in sys.argv
    if not SAVED_HOME.exists():
        raise FileNotFoundError(f"saved homepage not found: {SAVED_HOME}")

    html = SAVED_HOME.read_text(encoding="utf-8", errors="replace")
    grids = extract_grids(html)
    chunks = build_chunks(grids)

    for c in chunks:
        print(f"\n=== {c['chunk_id']}  (heading: {c['heading_path']})")
        print(c["text"])
    print(f"\n[quicklinks] popular={len(grids['popular'])} "
          f"portals={len(grids['portals'])} chunks={len(chunks)}")

    if not chunks:
        print("[quicklinks] nothing extracted — aborting.")
        return

    if commit:
        _rewrite_jsonl(chunks)
        _upsert(chunks)
        print("[quicklinks] committed to chunks.jsonl and Chroma.")
    else:
        print("\n[quicklinks] dry-run only. Re-run with --commit to index.")


if __name__ == "__main__":
    main()
