"""
fix_district_courts.py — add a clean "Cameron County District Courts" overview
chunk so natural questions ("What are the Cameron County District Courts?")
retrieve the real district-courts content.

Why this is needed: the district-courts page was crawled and IS indexed (rich
content: judges, staff, dockets), and it scores well above the relevance gate.
But on the natural phrasing the generic term "district" pulls the *District
Attorney* page and *Appraisal District* PDFs above it, crowding the actual
district-courts passages out of the top-k sent to the LLM — so the bot refused.

The fix mirrors add_homepage_quicklinks.py: build ONE concise overview chunk
whose heading is "Cameron County District Courts" and whose body enumerates the
courts and their judges (extracted verbatim from the already-crawled page — no
fabrication), then upsert it. Both distinctive terms ("district", "courts") sit
in the heading, so it ranks strongly for the natural question and gives the LLM
grounded material to answer from.

Idempotent: fixed chunk_id, so re-running replaces rather than duplicates.

    python fix_district_courts.py            # dry-run: print the overview
    python fix_district_courts.py --commit   # write chunk + update the index
"""
from __future__ import annotations

import json
import re
import sys

from bs4 import BeautifulSoup

import config

CHUNK_ID = "district-courts-overview"
DC_URL = "https://cameroncountytx.gov/district-courts"
DC_TITLE = "District Courts - Cameron County"
COURT_RE = re.compile(r"(\d{2,3}(?:st|nd|rd|th))\s*District Court")
# The name may sit on the line after "Honorable Judge" (so allow any whitespace
# before the first name word), but the name itself must stay on ONE line —
# otherwise the capture bleeds into following ALL-CAPS words like "IMPORTANT".
JUDGE_RE = re.compile(
    r"(?:Honorable\s+)?Judge\s+([A-Z][A-Za-z.\-]+(?:[ \t]+[A-Z][A-Za-z.\-]+){0,3})")


def _load_page_text() -> str:
    with config.PAGES_JSONL.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if (o.get("url") or "").lower().endswith("/district-courts"):
                soup = BeautifulSoup(o.get("raw_html", ""), "lxml")
                return soup.get_text("\n", strip=True).replace("\xa0", " ")
    raise SystemExit("district-courts page not found in pages.jsonl")


def _extract_pairs(text: str) -> list[tuple[str, str]]:
    """Pair each court with the first judge named in ITS section. A section runs
    from a court's FIRST mention to the first mention of the NEXT distinct court,
    so repeated mentions of the same court don't create empty spans and a judge
    can't bleed across courts."""
    first: dict[str, int] = {}
    for m in COURT_RE.finditer(text):
        first.setdefault(m.group(1), m.start())
    ordered = sorted(first.items(), key=lambda kv: kv[1])  # (court, pos) doc order

    pairs: list[tuple[str, str]] = []
    for i, (court, pos) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else len(text)
        span = text[pos:end]
        judge = ""
        for jm in JUDGE_RE.finditer(span):
            cand = jm.group(1).strip()
            if cand.lower() not in {"notice", "important"} and "District" not in cand:
                judge = cand
                break
        pairs.append((court, judge))
    pairs.sort(key=lambda p: int(re.match(r"\d+", p[0]).group()))
    return pairs


def _build_chunk(pairs: list[tuple[str, str]]) -> dict:
    lines = [
        "Cameron County District Courts. Cameron County is served by the "
        "following Texas state District Courts, each presided over by a judge:",
        "",
    ]
    for court, judge in pairs:
        if judge:
            lines.append(f"- {court} District Court of Cameron County — "
                         f"Honorable Judge {judge}")
        else:
            lines.append(f"- {court} District Court of Cameron County")
    lines.append("")
    lines.append("For dockets, court staff contacts, and case-information "
                 "inquiries, see each court's section on the county District "
                 "Courts page.")
    text = "\n".join(lines)
    return {
        "chunk_id": CHUNK_ID,
        "source_url": DC_URL,
        "page_title": DC_TITLE,
        "heading_path": "Cameron County District Courts",
        "text": text,
        "chunk_index": 0,
        "crawl_timestamp": "",
        "token_count": len(text.split()),
        "is_pdf": False,
    }


def _rewrite_jsonl(chunk: dict) -> None:
    path = config.CHUNKS_JSONL
    kept = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            if json.loads(line).get("chunk_id") == CHUNK_ID:
                continue
            kept.append(line.rstrip("\n"))
    with path.open("w", encoding="utf-8") as fh:
        for line in kept:
            fh.write(line + "\n")
        fh.write(json.dumps(chunk, ensure_ascii=False) + "\n")


def _upsert(chunk: dict) -> None:
    from embeddings import get_embedder
    from indexer import get_collection, _embed_text

    embedder = get_embedder()
    coll = get_collection(create=False)
    coll.upsert(
        ids=[chunk["chunk_id"]],
        embeddings=embedder.embed_documents([_embed_text(chunk)]),
        documents=[chunk["text"]],
        metadatas=[{
            "source_url": chunk["source_url"],
            "page_title": chunk["page_title"],
            "heading_path": chunk["heading_path"],
            "chunk_index": chunk["chunk_index"],
            "crawl_timestamp": chunk["crawl_timestamp"],
            "token_count": chunk["token_count"],
            "is_pdf": chunk["is_pdf"],
        }],
    )
    print(f"[dc] upserted overview chunk; collection count now {coll.count()}")


def main() -> None:
    commit = "--commit" in sys.argv
    text = _load_page_text()
    pairs = _extract_pairs(text)
    chunk = _build_chunk(pairs)

    print(f"[dc] extracted {len(pairs)} district courts:")
    for court, judge in pairs:
        print(f"    {court:6s} -> {judge or '(judge not matched)'}")
    print("\n[dc] overview chunk text:\n")
    print(chunk["text"])

    if commit:
        _rewrite_jsonl(chunk)
        _upsert(chunk)
        print("\n[dc] committed to chunks.jsonl and Chroma.")
    else:
        print("\n[dc] dry-run only. Re-run with --commit to index.")


if __name__ == "__main__":
    main()
