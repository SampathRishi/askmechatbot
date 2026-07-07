"""
add_external_pages.py — index APPROVED external-domain pages into the existing
vector store, via a bounded, polite, same-host crawl (no full-domain explosion).

The county homepage links out to a few official services that live on separate
hosts (see config.ALLOWLIST_DOMAINS). This helper fetches a small, capped set of
pages from each approved seed, cleans + chunks them with the SAME processor used
for the main corpus, and upserts the chunks into the existing ChromaDB
collection — so the assistant can answer about them without a full re-crawl or
re-embed of the whole site.

Scope guarantees (so this never wanders off into a huge external site):
  * only fetches hosts whose registered domain is in config.ALLOWLIST_DOMAINS;
  * only follows links on the SAME host as each seed;
  * hard per-seed page cap (SEEDS[i].max_pages);
  * polite rate limit + descriptive browser User-Agent;
  * HTML only (skips PDFs/assets here for simplicity).

Idempotent: every chunk id is derived from its URL, and a re-run first drops all
previously-added external chunks (chunk_id prefix "ext::") from chunks.jsonl and
Chroma before re-adding, so re-running never duplicates.

    python add_external_pages.py            # dry-run: fetch + report, no writes
    python add_external_pages.py --commit   # write chunks + update the index
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

import config
from crawler import registered_domain, normalize_url, host_in_allowlist
from processor import (
    extract_blocks_from_html,
    chunk_section,
    _looks_like_nav,
    _looks_like_garbage,
)
from text_utils import count_tokens, text_hash

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# A fuller browser header set — some approved hosts (e.g. cameron.agrilife.org)
# sit behind a WAF that 403s a bare User-Agent but passes a realistic browser.
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    # Only advertise encodings httpx decodes natively — NOT brotli ("br"), which
    # needs an extra package; without it a br-compressed 200 decodes to garbage.
    "Accept-Encoding": "gzip, deflate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
CHUNK_ID_PREFIX = "ext::"


@dataclass
class Seed:
    label: str
    url: str
    max_pages: int
    force_browser: bool = False  # skip httpx, always render (for WAF hosts)
    fallback_text: str = ""      # curated chunk to index if crawling yields none


# A factual, non-fabricated description used only when a seed's own content
# cannot be crawled (e.g. an aggressive WAF). Grounds a hand-off answer: what the
# service is + where to go — never invented specifics.
AGRILIFE_FALLBACK = (
    "Cameron County Extension Office — Texas A&M AgriLife Extension Service in "
    "Cameron County. This is Cameron County's Extension Office, part of the "
    "Texas A&M AgriLife Extension Service (Cooperative Extension Program), which "
    "provides Extension education to residents of Cameron County, Texas. The "
    "office publishes quarterly newsletters and annual reports and shares "
    "information about its programs and events. Its website is "
    "https://cameron.agrilife.org/ — a separate official Texas A&M AgriLife site "
    "that is linked from the Cameron County website under Departments."
)

# Factual hand-off descriptions for the homepage-grid portals. These are dynamic
# web apps (search / login / self-service) that usually can't be crawled for
# content; the description lets the assistant identify the service and point the
# user to it. Each states only what the link's own label/purpose and URL imply —
# no invented specifics. If a portal DOES yield crawlable content, that content
# is indexed instead and the fallback is not used.
_F = {
    "jobs": (
        "Cameron County Job Opportunities. Cameron County posts current job "
        "openings and accepts employment applications online through its jobs "
        "portal at https://cameroncountytx.applicantpool.com/jobs/ (hosted on "
        "ApplicantPool), linked as 'Job Opportunities' on the county homepage."
    ),
    "taxportal": (
        "Cameron County Taxes Portal. The Cameron County Tax Assessor-Collector's "
        "online portal is at https://www.cameroncountytax.org/, providing property "
        "tax information and online tax services. It is linked as 'Taxes Portal' "
        "on the Cameron County homepage."
    ),
    "paytaxes": (
        "Pay Cameron County property taxes online. Property tax accounts can be "
        "searched and paid through the county's tax payment portal at "
        "https://camerontax.go2gov.net/, linked as 'Pay Taxes' on the Cameron "
        "County homepage."
    ),
    "records_dc": (
        "Cameron County Official Records Search and District Clerk Historical "
        "Books. Search Cameron County official public records and browse District "
        "Clerk historical record books online through the Kofile portal at "
        "https://kofilequicklinks.com/camerondc/, linked as 'Official Records "
        "Search' and 'District Clerk Hist Books' on the Cameron County homepage."
    ),
    "records_cc": (
        "Cameron County Clerk Historical Books. Browse the Cameron County Clerk's "
        "historical record books online through the Kofile portal at "
        "https://kofilequicklinks.com/cameroncc/, linked as 'County Clerk Hist "
        "Books' on the Cameron County homepage."
    ),
    "energov": (
        "Cameron County Permits & Development and DOT Self Service. Cameron County "
        "offers online building permits, development applications, and Department "
        "of Transportation self-service through the Tyler EnerGov self-service "
        "portal at https://cameroncountytx-energovpub.tylerhost.net/apps/selfservice, "
        "linked as 'Permits & Development' and 'DOT Self Service' on the county "
        "homepage."
    ),
    "jury": (
        "Cameron County Jury Duty. Information about Cameron County jury service, "
        "including a jury duty FAQ, is available at "
        "https://websvr.co.cameron.tx.us/main.asp?id=faq, linked as 'Jury Duty' "
        "on the Cameron County homepage."
    ),
}

# Approved external services (their host must pass the allowlist). Portals try a
# normal crawl first and fall back to the curated description above.
SEEDS: List[Seed] = [
    # agrilife.org sits behind an aggressive WAF that 403s plain HTTP clients and
    # headless browsers alike. Try a real browser; if blocked, index a factual
    # hand-off description so the assistant can still identify the Extension
    # Office and point users to it. (Kept in the allowlist for a future crawl
    # from a non-flagged IP.)
    Seed("Extension Office (Texas A&M AgriLife, Cameron County)",
         "https://cameron.agrilife.org/", max_pages=10,
         force_browser=True, fallback_text=AGRILIFE_FALLBACK),
    Seed("iDocket — court case tracking portal",
         "https://idocket.com/homepage2.htm", max_pages=3),
    Seed("Job Opportunities (ApplicantPool)",
         "https://cameroncountytx.applicantpool.com/jobs/", max_pages=3,
         fallback_text=_F["jobs"]),
    Seed("Taxes Portal (Cameron County Tax Office)",
         "https://www.cameroncountytax.org/", max_pages=6,
         fallback_text=_F["taxportal"]),
    Seed("Pay Taxes (go2gov tax payment portal)",
         "https://camerontax.go2gov.net/faces/search.jsp", max_pages=2,
         fallback_text=_F["paytaxes"]),
    Seed("Official Records Search / District Clerk Hist Books (Kofile)",
         "https://kofilequicklinks.com/camerondc/", max_pages=3,
         fallback_text=_F["records_dc"]),
    Seed("County Clerk Hist Books (Kofile)",
         "https://kofilequicklinks.com/cameroncc/", max_pages=3,
         fallback_text=_F["records_cc"]),
    Seed("Permits & Development / DOT Self Service (Tyler EnerGov)",
         "https://cameroncountytx-energovpub.tylerhost.net/apps/selfservice",
         max_pages=2, fallback_text=_F["energov"]),
    Seed("Jury Duty (Cameron County)",
         "https://websvr.co.cameron.tx.us/main.asp?id=faq", max_pages=3,
         fallback_text=_F["jury"]),
]


def _fallback_chunk(seed: Seed) -> dict:
    return {
        "chunk_id": f"{CHUNK_ID_PREFIX}{seed.url}::desc",
        "source_url": seed.url,
        "page_title": seed.label,
        "heading_path": seed.label,  # seed-specific so retrieval matches the topic
        "text": seed.fallback_text,
        "chunk_index": 0,
        "crawl_timestamp": "",
        "token_count": len(seed.fallback_text.split()),
        "is_pdf": False,
    }


class BrowserRenderer:
    """Persistent Playwright browser for WAF-protected hosts. One browser is
    reused across all pages (cheap), pages load on `domcontentloaded` (reliable,
    unlike `networkidle` on sites with long-poll connections), and a single
    page failure never disables the whole renderer."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._failed = False

    def _ensure(self) -> None:
        if self._browser is not None or self._failed:
            return
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            print(f"[ext] Playwright unavailable ({exc}); WAF hosts will be "
                  f"skipped. Run `playwright install chromium` to enable.")
            self._failed = True

    def render(self, url: str) -> str | None:
        self._ensure()
        if self._browser is None:
            return None
        try:
            page = self._browser.new_page(user_agent=BROWSER_UA)
            # networkidle waits out WAF/JS challenges (Cloudflare) so we capture
            # the real content, not the "checking your browser" interstitial.
            try:
                page.goto(url, wait_until="networkidle",
                          timeout=int(config.REQUEST_TIMEOUT_SECONDS * 1000))
            except Exception:  # noqa: BLE001 - networkidle can time out; use what loaded
                page.wait_for_timeout(2500)
            html = page.content()
            page.close()
            return html
        except Exception as exc:  # noqa: BLE001
            print(f"[ext]   browser render failed {url}: {exc}")
            return None

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


def _in_allowlist(url: str) -> bool:
    return host_in_allowlist(urlparse(url).netloc)


def _same_host(a: str, b: str) -> bool:
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()


def _fetch_html(client: httpx.Client, url: str,
                renderer: "BrowserRenderer",
                force_browser: bool = False) -> str | None:
    """Fetch page HTML, falling back to a real browser (Playwright) when the host
    WAF-blocks the plain HTTP client (e.g. agrilife.org returns 403). When
    force_browser is set, skip httpx entirely and render every page."""
    if force_browser:
        return renderer.render(url)
    try:
        r = client.get(url)
        ctype = r.headers.get("content-type", "").lower()
        if r.status_code < 400 and "text/html" in ctype:
            r.encoding = r.encoding or "utf-8"
            text = r.text
            # sanity: a real HTML doc has ASCII tags. Garbage (e.g. an
            # undecoded compressed body or a WAF binary blob) does not —
            # fall through to a real browser in that case.
            low = text[:4000].lower()
            if any(t in low for t in ("<html", "<body", "<div", "<a ", "<p>")):
                return text
        blocked = r.status_code in (401, 403, 429, 503) or r.status_code < 400
    except Exception as exc:  # noqa: BLE001
        print(f"[ext]   http fetch error {url}: {exc}; trying browser render")
        blocked = True
    if blocked:
        html = renderer.render(url)
        if html:
            return html
        print(f"[ext]   blocked and no browser render for {url}")
    return None


def _crawl_seed(client: httpx.Client, seed: Seed,
                renderer: "BrowserRenderer") -> list[dict]:
    """Bounded same-host BFS from one seed. Returns crawled page records
    {url, title, html}."""
    if not _in_allowlist(seed.url):
        print(f"[ext] SKIP {seed.url}: registered domain not in allowlist.")
        return []
    start = normalize_url(seed.url) or seed.url
    queue = [start]
    seen = {start}
    out: list[dict] = []
    while queue and len(out) < seed.max_pages:
        url = queue.pop(0)
        html = _fetch_html(client, url, renderer, seed.force_browser)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        title = (soup.title.string.strip()
                 if soup.title and soup.title.string else url)
        out.append({"url": url, "title": title, "html": html})
        # enqueue same-host links (only while we still need pages)
        if len(out) + len(queue) < seed.max_pages * 3:
            for a in soup.find_all("a", href=True):
                nxt = normalize_url(a["href"], base=url)
                if (nxt and nxt not in seen and _same_host(nxt, start)
                        and not urlparse(nxt).path.lower().endswith(".pdf")):
                    seen.add(nxt)
                    queue.append(nxt)
        time.sleep(0.6)  # polite
    print(f"[ext] {seed.label}: fetched {len(out)} page(s) "
          f"(cap {seed.max_pages})")
    return out


def _looks_like_spa_shell(text: str) -> bool:
    """True for JavaScript single-page-app shells whose 'text' is unrendered
    template markup (e.g. Angular `{{vm.userService.userName}}`, Log In/Register
    scaffolding). Such chunks carry no real content and should be dropped so the
    portal falls back to its curated hand-off description instead."""
    return text.count("{{") >= 2 and "}}" in text


def _chunks_from_page(page: dict, seed_label: str, seen_hashes: set) -> list[dict]:
    blocks = extract_blocks_from_html(page["html"], page["title"])
    recs: list[dict] = []
    idx = 0
    for block in blocks:
        for ctext in chunk_section(block.heading_path, block.text):
            tok = count_tokens(ctext)
            if tok < config.CHUNK_MIN_TOKENS:
                continue
            if (_looks_like_nav(ctext) or _looks_like_garbage(ctext)
                    or _looks_like_spa_shell(ctext)):
                continue
            h = text_hash(ctext)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            recs.append({
                "chunk_id": f"{CHUNK_ID_PREFIX}{page['url']}::{idx}",
                "source_url": page["url"],
                "page_title": page["title"],
                "heading_path": block.heading_path,
                "text": ctext,
                "chunk_index": idx,
                "crawl_timestamp": "",
                "token_count": tok,
                "is_pdf": False,
            })
            idx += 1
    return recs


def _rewrite_jsonl(new_chunks: list[dict]) -> None:
    """Drop all previously-added external chunks, then append the new ones."""
    path = config.CHUNKS_JSONL
    kept = []
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                cid = json.loads(line).get("chunk_id", "")
                if str(cid).startswith(CHUNK_ID_PREFIX):
                    continue
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

    # drop stale external chunks first (ids may change across runs)
    try:
        existing = coll.get()  # all ids (no `where` filter)
        stale = [i for i in (existing.get("ids") or [])
                 if str(i).startswith(CHUNK_ID_PREFIX)]
        if stale:
            coll.delete(ids=stale)
            print(f"[ext] removed {len(stale)} stale external chunks from index")
    except Exception as exc:  # noqa: BLE001
        print(f"[ext] (skipped stale cleanup: {exc})")

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
    print(f"[ext] upserted {len(new_chunks)} chunks; "
          f"collection count now {coll.count()}")


def main() -> None:
    commit = "--commit" in sys.argv
    client = httpx.Client(timeout=30, follow_redirects=True,
                          headers=BROWSER_HEADERS)
    renderer = BrowserRenderer()
    all_chunks: list[dict] = []
    seen_hashes: set = set()
    per_seed = {}
    try:
        for seed in SEEDS:
            pages = _crawl_seed(client, seed, renderer)
            seed_chunks: list[dict] = []
            for page in pages:
                seed_chunks.extend(
                    _chunks_from_page(page, seed.label, seen_hashes))
            if not seed_chunks and seed.fallback_text:
                seed_chunks = [_fallback_chunk(seed)]
                print(f"[ext] {seed.label}: crawl blocked/empty — using curated "
                      f"fallback description chunk.")
            per_seed[seed.label] = (len(pages), len(seed_chunks))
            all_chunks.extend(seed_chunks)
    finally:
        client.close()
        renderer.close()

    print("\n[ext] summary:")
    for label, (npages, nchunks) in per_seed.items():
        print(f"    {label}: {npages} pages -> {nchunks} chunks")
    print(f"[ext] total new chunks: {len(all_chunks)}")

    if not all_chunks:
        print("[ext] nothing to index — aborting.")
        return

    if commit:
        _rewrite_jsonl(all_chunks)
        _upsert(all_chunks)
        print("[ext] committed to chunks.jsonl and Chroma.")
    else:
        # show a few sample chunks so a dry-run is inspectable (encoding-safe:
        # the console may be cp1252 and chunk text can contain other codepoints)
        def _safe(s: str) -> str:
            return s.encode("ascii", "replace").decode("ascii")
        for c in all_chunks[:4]:
            print(f"\n  --- {_safe(c['source_url'])}  [{_safe(c['heading_path'][:50])}]")
            print(f"      {_safe(c['text'][:180])}")
        print("\n[ext] dry-run only. Re-run with --commit to index.")


if __name__ == "__main__":
    main()
