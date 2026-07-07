"""
coverage.py — report exactly what made it into the index.

Reads data/raw/pages.jsonl (everything crawled) and data/processed/chunks.jsonl
(everything indexed), and writes data/coverage_report.txt:

  * every HTML page in the index, with its chunk count
  * every PDF in the index, with its chunk count
  * pages that were crawled but produced no indexed chunks (thin/empty)

Also supports a quick single-URL lookup:
    python coverage.py https://www.cameroncountytx.gov/some-page/
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from urllib.parse import urlparse

import config


def _norm(url: str) -> str:
    p = urlparse(url)
    host = p.netloc.lower().replace("www.", "")
    path = p.path.rstrip("/") or "/"
    return f"{host}{path}"


def _load_chunks_by_url():
    counts: dict[str, int] = defaultdict(int)
    is_pdf: dict[str, bool] = {}
    titles: dict[str, str] = {}
    with config.CHUNKS_JSONL.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            c = json.loads(line)
            u = c["source_url"]
            counts[u] += 1
            is_pdf[u] = c.get("is_pdf", False)
            titles.setdefault(u, c.get("page_title", ""))
    return counts, is_pdf, titles


def _load_crawled_urls():
    urls = {}
    with config.PAGES_JSONL.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            urls[r["url"]] = r.get("is_pdf", False)
    return urls


def lookup(url: str) -> None:
    counts, is_pdf, _ = _load_chunks_by_url()
    crawled = _load_crawled_urls()
    key = _norm(url)
    crawled_match = [u for u in crawled if _norm(u) == key]
    indexed_match = [u for u in counts if _norm(u) == key]
    print(f"URL: {url}")
    print(f"  crawled : {bool(crawled_match)}  {crawled_match[0] if crawled_match else ''}")
    print(f"  indexed : {bool(indexed_match)}  "
          f"({counts[indexed_match[0]] if indexed_match else 0} chunks)")


def report() -> None:
    counts, is_pdf, titles = _load_chunks_by_url()
    crawled = _load_crawled_urls()

    html = sorted((u for u in counts if not is_pdf[u]))
    pdfs = sorted((u for u in counts if is_pdf[u]))
    indexed_set = set(counts)
    crawled_not_indexed = sorted(u for u in crawled if u not in indexed_set and not crawled[u])

    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("CAMERON COUNTY RAG — COVERAGE REPORT")
    lines.append("=" * 70)
    lines.append(f"HTML pages indexed : {len(html)}")
    lines.append(f"PDFs indexed       : {len(pdfs)}")
    lines.append(f"Total indexed URLs : {len(indexed_set)}")
    lines.append(f"Total chunks       : {sum(counts.values())}")
    lines.append(f"Crawled but empty  : {len(crawled_not_indexed)} (thin/redirect pages)")
    lines.append("")
    lines.append("-" * 70)
    lines.append("HTML PAGES (chunks | url)")
    lines.append("-" * 70)
    for u in html:
        lines.append(f"{counts[u]:>4} | {u}")
    lines.append("")
    lines.append("-" * 70)
    lines.append("PDFs (chunks | url)")
    lines.append("-" * 70)
    for u in pdfs:
        lines.append(f"{counts[u]:>4} | {u}")
    lines.append("")
    lines.append("-" * 70)
    lines.append("CRAWLED BUT NOT INDEXED (thin / no extractable content)")
    lines.append("-" * 70)
    for u in crawled_not_indexed:
        lines.append(f"     | {u}")

    out = config.DATA_DIR / "coverage_report.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    # print the summary to console
    for l in lines[:12]:
        print(l)
    print(f"\nFull report written to: {out}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].startswith("http"):
        lookup(sys.argv[1])
    else:
        report()
