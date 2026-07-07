"""
crawler.py — Phase 1: website-grounded crawler for cameroncountytx.gov.

Entry point: `crawl_site(base_url)`.

Behaviour (see config.py for every tunable):
  * Seeds the URL queue from sitemap.xml / wp-sitemap.xml / sitemap_index.xml
    (following sitemap-index files to their child sitemaps).
  * BFS crawl of internal links only (same registered domain; www/bare and
    http/https treated as one). URLs are normalized (fragments stripped,
    relatives resolved, trailing slash + scheme + host canonicalized).
  * Respects robots.txt and rate-limits to <= REQUESTS_PER_SECOND with a
    descriptive User-Agent.
  * Skips non-HTML assets, but extracts text from same-domain PDFs via pypdf
    (with size + count caps; skipped PDFs are logged).
  * JS-render detection: pages with almost no text but large HTML are retried
    with Playwright when available.
  * Retries failed fetches (exponential backoff), logs permanent failures, and
    never lets one bad page crash the whole crawl.
  * Persists one record per page/PDF to data/raw/pages.jsonl and prints a
    crawl report (also written to data/raw/crawl_report.json).

Record schema (one JSON object per line in pages.jsonl):
    {url, title, raw_html, fetch_timestamp, http_status, content_type,
     text_len, needed_js, is_pdf}
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup

import config


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #

def registered_domain(host: str) -> str:
    """Return the registered domain (last two labels) of a hostname, lowercased.

    Good enough for `.gov`/`.us`/`.org` hosts we care about here; strips a
    leading `www.` implicitly because we only compare the last two labels.
    """
    host = (host or "").lower().strip()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    return ".".join(parts[-2:])


def normalize_url(url: str, base: Optional[str] = None) -> Optional[str]:
    """Resolve, clean and canonicalize a URL. Returns None if not http(s).

    - resolves relative URLs against `base`
    - forces https
    - lowercases the host and drops a leading `www.`
    - strips the fragment and any trailing `?`/empty query
    - removes a trailing slash (except for the bare root)
    """
    if not url:
        return None
    url = url.strip()
    if url.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    # Reject bare email addresses linked without a mailto: scheme (common on this
    # site's microsites) — otherwise urljoin turns them into dead relative paths.
    if "://" not in url and re.match(r"^[^\s/]+@[^\s/]+\.[A-Za-z]{2,}$", url):
        return None
    if base:
        url = urljoin(base, url)
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None

    host = parts.netloc.lower()
    # strip credentials/port artifacts and leading www.
    if "@" in host:
        host = host.split("@", 1)[1]
    if host.startswith("www."):
        host = host[4:]

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = parts.query  # keep meaningful queries; skip logic handled elsewhere

    return urlunparse(("https", host, path, "", query, ""))


def host_in_allowlist(host: str) -> bool:
    """True if `host` is allowed by the configured allowlist. An allowlist entry
    matches either as a registered domain (covers all subdomains) or as an exact
    full host / host-suffix (keeps broad public suffixes like `tx.us` scoped to a
    single host, e.g. `websvr.co.cameron.tx.us`)."""
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if registered_domain(host) in config.ALLOWLIST_DOMAINS:
        return True
    for entry in config.ALLOWLIST_DOMAINS:
        if host == entry or host.endswith("." + entry):
            return True
    return False


def is_in_scope(url: str) -> bool:
    """True if the URL's host is allowed by the configured allowlist."""
    return host_in_allowlist(urlparse(url).netloc)


def external_domain(url: str) -> Optional[str]:
    """Return the registered domain if the URL is OUT of scope, else None."""
    host = urlparse(url).netloc
    if host_in_allowlist(host):
        return None
    return registered_domain(host)


def _path_and_query(url: str) -> str:
    p = urlparse(url)
    return (p.path + ("?" + p.query if p.query else "")).lower()


def should_skip_url(url: str) -> bool:
    """True if the URL matches an infinite/low-value trap pattern."""
    pq = _path_and_query(url)

    for sub in config.SKIP_URL_SUBSTRINGS:
        if sub.lower() in pq:
            return True
    for rx in config.SKIP_URL_REGEXES:
        if re.search(rx, pq):
            return True

    # tag/category/author archive pagination beyond MAX_ARCHIVE_PAGE
    m = re.search(r"/(?:category|tag|author)/[^?]*?/page/(\d+)", pq)
    if m and int(m.group(1)) > config.MAX_ARCHIVE_PAGE:
        return True
    # generic /page/N/ deep pagination beyond the cap
    m = re.search(r"/page/(\d+)", pq)
    if m and int(m.group(1)) > config.MAX_ARCHIVE_PAGE:
        return True
    return False


def is_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


def is_skippable_asset(url: str) -> bool:
    ext = "." + urlparse(url).path.rsplit(".", 1)[-1].lower() if "." in urlparse(url).path else ""
    return ext in config.SKIP_ASSET_EXTENSIONS


def priority_score(url: str) -> int:
    """Lower score = crawled sooner. Department/service pages sort first,
    deep agenda/archive pages sort last."""
    pq = _path_and_query(url)
    score = 0
    if any(h in pq for h in config.PRIORITY_PATH_HINTS):
        score -= 10
    if any(h in pq for h in config.DEPRIORITY_PATH_HINTS):
        score += 10
    return score


# --------------------------------------------------------------------------- #
# Crawl state / report
# --------------------------------------------------------------------------- #

@dataclass
class CrawlReport:
    started_at: str = ""
    finished_at: str = ""
    pages_crawled: int = 0
    pdfs_extracted: int = 0
    pages_failed: int = 0
    needed_js: int = 0
    unique_urls_discovered: int = 0
    urls_from_sitemaps: int = 0
    pdfs_skipped_too_large: int = 0
    pdfs_skipped_cap: int = 0
    page_cap_hit: bool = False
    pdf_cap_hit: bool = False
    external_domains: dict = field(default_factory=dict)   # domain -> count
    skipped_low_value: int = 0

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["external_domains"] = dict(
            sorted(self.external_domains.items(), key=lambda kv: -kv[1])
        )
        return d


# --------------------------------------------------------------------------- #
# The crawler
# --------------------------------------------------------------------------- #

class Crawler:
    def __init__(self, base_url: str):
        self.base_url = normalize_url(base_url) or base_url
        self.report = CrawlReport(started_at=_now_iso())
        self.seen: set[str] = set()          # normalized URLs already queued
        self.visited: set[str] = set()       # normalized URLs already fetched
        self.queue: deque[str] = deque()
        self.client = httpx.Client(
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        self._robots: Optional[RobotFileParser] = None
        self._last_request_ts = 0.0
        self._min_interval = 1.0 / max(config.REQUESTS_PER_SECOND, 0.01)
        # truncate output files for a clean run
        config.PAGES_JSONL.write_text("", encoding="utf-8")
        config.FAILED_URLS_TXT.write_text("", encoding="utf-8")
        self._pages_fh = config.PAGES_JSONL.open("a", encoding="utf-8")

    # ----- politeness -----------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_ts = time.monotonic()

    def _load_robots(self) -> None:
        if not config.RESPECT_ROBOTS_TXT:
            return
        robots_url = urljoin(self.base_url, "/robots.txt")
        rp = RobotFileParser()
        try:
            self._throttle()
            resp = self.client.get(robots_url)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp.parse([])  # nothing disallowed
        except Exception as exc:  # noqa: BLE001 - robots is best-effort
            print(f"[robots] could not fetch robots.txt ({exc}); proceeding.")
            rp.parse([])
        self._robots = rp

    def _allowed_by_robots(self, url: str) -> bool:
        if not config.RESPECT_ROBOTS_TXT or self._robots is None:
            return True
        try:
            return self._robots.can_fetch(config.USER_AGENT, url)
        except Exception:  # noqa: BLE001
            return True

    # ----- fetching with retries -----------------------------------------

    def _get(self, url: str, *, stream_head_only: bool = False) -> Optional[httpx.Response]:
        """GET with retries + exponential backoff. Returns Response or None."""
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                self._throttle()
                resp = self.client.get(url)
                # retry only on transient server errors
                if resp.status_code >= 500 and attempt < config.MAX_RETRIES:
                    raise httpx.HTTPStatusError(
                        "server error", request=resp.request, response=resp
                    )
                return resp
            except Exception as exc:  # noqa: BLE001 - resilient by design
                if attempt >= config.MAX_RETRIES:
                    print(f"[fetch] giving up on {url}: {exc}")
                    return None
                backoff = config.RETRY_BACKOFF_BASE ** attempt
                print(f"[fetch] retry {attempt}/{config.MAX_RETRIES} for {url} "
                      f"in {backoff:.1f}s ({exc})")
                time.sleep(backoff)
        return None

    # ----- sitemap seeding ------------------------------------------------

    def seed_from_sitemaps(self) -> None:
        found: list[str] = []
        for candidate in config.SITEMAP_CANDIDATES:
            sm_url = normalize_url(urljoin(self.base_url, candidate))
            if not sm_url:
                continue
            urls = self._parse_sitemap(sm_url, depth=0)
            if urls:
                print(f"[sitemap] {sm_url}: {len(urls)} URLs")
                found.extend(urls)
        # de-dupe, normalize, enqueue in-scope
        added = 0
        for u in found:
            nu = normalize_url(u)
            if nu and is_in_scope(nu) and nu not in self.seen:
                self._enqueue(nu)
                added += 1
        self.report.urls_from_sitemaps = added
        print(f"[sitemap] seeded {added} in-scope URLs from sitemaps")

    def _parse_sitemap(self, sm_url: str, depth: int) -> list[str]:
        """Return page URLs from a sitemap or sitemap-index (recursively)."""
        if depth > 5:
            return []
        resp = self._get(sm_url)
        if resp is None or resp.status_code != 200:
            return []
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return []
        # strip namespaces for simpler matching
        tag = root.tag.split("}")[-1]
        locs = [
            el.text.strip()
            for el in root.iter()
            if el.tag.split("}")[-1] == "loc" and el.text
        ]
        if tag == "sitemapindex":
            child_urls: list[str] = []
            for child in locs:
                child_urls.extend(self._parse_sitemap(child, depth + 1))
            return child_urls
        return locs  # urlset

    # ----- queue management ----------------------------------------------

    def _enqueue(self, url: str) -> None:
        if url in self.seen:
            return
        self.seen.add(url)
        # priority: department/service pages to the front, archives to the back
        score = priority_score(url)
        if score < 0:
            self.queue.appendleft(url)
        elif score > 0:
            self.queue.append(url)
        else:
            self.queue.append(url)

    # ----- link + PDF extraction -----------------------------------------

    def _extract_links(self, html: str, page_url: str) -> Iterable[str]:
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            raw = a["href"]
            nu = normalize_url(raw, base=page_url)
            if not nu:
                continue
            ext_dom = external_domain(nu)
            if ext_dom:
                self.report.external_domains[ext_dom] = (
                    self.report.external_domains.get(ext_dom, 0) + 1
                )
                continue  # never crawl off-domain
            yield nu

    # ----- record writing -------------------------------------------------

    def _write_record(self, record: dict) -> None:
        self._pages_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._pages_fh.flush()

    def _log_failure(self, url: str, reason: str) -> None:
        self.report.pages_failed += 1
        with config.FAILED_URLS_TXT.open("a", encoding="utf-8") as fh:
            fh.write(f"{url}\t{reason}\n")

    # ----- page handling --------------------------------------------------

    def _handle_pdf(self, url: str, resp: httpx.Response) -> None:
        # Respect the total-PDF cap.
        if self.report.pdfs_extracted >= config.MAX_PDFS:
            self.report.pdf_cap_hit = True
            self.report.pdfs_skipped_cap += 1
            return
        size_mb = len(resp.content) / (1024 * 1024)
        if size_mb > config.MAX_PDF_SIZE_MB:
            self.report.pdfs_skipped_too_large += 1
            self._log_failure(url, f"pdf-too-large:{size_mb:.1f}MB")
            print(f"[pdf] skip (>{config.MAX_PDF_SIZE_MB}MB): {url}")
            return
        text = extract_pdf_text(resp.content)
        if not text.strip():
            self._log_failure(url, "pdf-no-text")
            return
        title = urlparse(url).path.rsplit("/", 1)[-1]
        self._write_record({
            "url": url,
            "title": title,
            "raw_html": text,          # store extracted PDF text in raw_html slot
            "fetch_timestamp": _now_iso(),
            "http_status": resp.status_code,
            "content_type": "application/pdf",
            "text_len": len(text),
            "needed_js": False,
            "is_pdf": True,
        })
        self.report.pdfs_extracted += 1
        if self.report.pdfs_extracted % 25 == 0:
            print(f"[pdf] extracted {self.report.pdfs_extracted} PDFs")

    def _handle_html(self, url: str, resp: httpx.Response) -> None:
        html = resp.text
        soup = BeautifulSoup(html, "lxml")
        title = (soup.title.string.strip() if soup.title and soup.title.string else "")
        visible_text = soup.get_text(" ", strip=True)
        needed_js = False

        # JS-render detection + optional Playwright fallback. We render when the
        # page is configured to always render, has almost no text, OR its visible
        # text is a tiny fraction of its HTML (JS-assembled content the static
        # fetch missed — the trigger the old char-only threshold never caught).
        text_html_ratio = len(visible_text) / max(len(html), 1)
        needs_render = config.ENABLE_PLAYWRIGHT and (
            config.RENDER_ALL_HTML
            or len(visible_text) < config.JS_RENDER_MIN_TEXT_CHARS
            or (len(html) >= config.JS_RENDER_MIN_HTML_BYTES
                and text_html_ratio < config.JS_RENDER_MAX_TEXT_HTML_RATIO)
        )
        if needs_render:
            rendered = render_with_playwright(url)
            if rendered:
                html = rendered
                soup = BeautifulSoup(html, "lxml")
                title = (soup.title.string.strip()
                         if soup.title and soup.title.string else title)
                visible_text = soup.get_text(" ", strip=True)
                needed_js = True
                self.report.needed_js += 1

        # enqueue discovered internal links (respecting caps/skips)
        for link in self._extract_links(html, url):
            if link in self.seen:
                continue
            if should_skip_url(link):
                self.report.skipped_low_value += 1
                continue
            if is_pdf_url(link) or (not is_skippable_asset(link)):
                self._enqueue(link)

        self._write_record({
            "url": url,
            "title": title,
            "raw_html": html,
            "fetch_timestamp": _now_iso(),
            "http_status": resp.status_code,
            "content_type": resp.headers.get("content-type", "text/html"),
            "text_len": len(visible_text),
            "needed_js": needed_js,
            "is_pdf": False,
        })
        self.report.pages_crawled += 1
        if self.report.pages_crawled % 50 == 0:
            print(f"[crawl] {self.report.pages_crawled} pages | "
                  f"queue={len(self.queue)} | pdfs={self.report.pdfs_extracted}")

    # ----- main loop ------------------------------------------------------

    def run(self) -> CrawlReport:
        self._load_robots()
        self.seed_from_sitemaps()
        # always ensure the base URL itself is queued
        if self.base_url not in self.seen:
            self._enqueue(self.base_url)

        while self.queue:
            if self.report.pages_crawled >= config.MAX_PAGES:
                self.report.page_cap_hit = True
                print(f"[crawl] page cap ({config.MAX_PAGES}) reached; stopping.")
                break

            url = self.queue.popleft()
            if url in self.visited:
                continue
            self.visited.add(url)

            if not is_in_scope(url):
                continue
            if should_skip_url(url):
                self.report.skipped_low_value += 1
                continue
            if not self._allowed_by_robots(url):
                self._log_failure(url, "robots-disallow")
                continue

            is_pdf = is_pdf_url(url)
            if (not is_pdf) and is_skippable_asset(url):
                continue

            resp = self._get(url)
            if resp is None:
                self._log_failure(url, "fetch-failed")
                continue
            if resp.status_code >= 400:
                self._log_failure(url, f"http-{resp.status_code}")
                continue

            content_type = resp.headers.get("content-type", "").lower()
            try:
                if is_pdf or "application/pdf" in content_type:
                    self._handle_pdf(url, resp)
                elif "text/html" in content_type or "xhtml" in content_type or content_type == "":
                    self._handle_html(url, resp)
                else:
                    # unknown/non-HTML content type — skip but don't fail
                    continue
            except Exception as exc:  # noqa: BLE001 - never crash the crawl
                self._log_failure(url, f"handler-error:{type(exc).__name__}:{exc}")
                print(f"[crawl] error handling {url}: {exc}")

        self.report.unique_urls_discovered = len(self.seen)
        self.report.finished_at = _now_iso()
        self._pages_fh.close()
        self.client.close()
        return self.report


# --------------------------------------------------------------------------- #
# PDF + Playwright helpers (import-light; heavy deps loaded lazily)
# --------------------------------------------------------------------------- #

def extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # noqa: BLE001
        print(f"[pdf] pypdf unavailable: {exc}")
        return ""
    try:
        reader = PdfReader(BytesIO(data))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - skip bad page, keep going
                continue
        return "\n".join(parts)
    except Exception as exc:  # noqa: BLE001
        print(f"[pdf] extract failed: {exc}")
        return ""


_PLAYWRIGHT_UNAVAILABLE = False  # set True after first failure to avoid retry storms


def render_with_playwright(url: str) -> Optional[str]:
    """Render a JS page and return its HTML, or None if Playwright is not
    installed / fails. A missing browser is logged once, not per-page."""
    global _PLAYWRIGHT_UNAVAILABLE
    if _PLAYWRIGHT_UNAVAILABLE:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        _PLAYWRIGHT_UNAVAILABLE = True
        print("[js] Playwright not installed; keeping raw HTML. "
              "Run `playwright install chromium` to enable JS rendering.")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=config.USER_AGENT)
            page.goto(url, wait_until="networkidle",
                      timeout=int(config.REQUEST_TIMEOUT_SECONDS * 1000))
            html = page.content()
            browser.close()
            return html
    except Exception as exc:  # noqa: BLE001
        _PLAYWRIGHT_UNAVAILABLE = True
        print(f"[js] Playwright render failed ({exc}); disabling JS fallback. "
              "Run `playwright install chromium` if you need it.")
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def crawl_site(base_url: str = config.BASE_URL) -> CrawlReport:
    crawler = Crawler(base_url)
    report = crawler.run()
    config.CRAWL_REPORT_JSON.write_text(
        json.dumps(report.to_dict(), indent=2), encoding="utf-8"
    )
    _print_report(report)
    return report


def _print_report(r: CrawlReport) -> None:
    print("\n" + "=" * 64)
    print("CRAWL REPORT")
    print("=" * 64)
    print(f"  started            : {r.started_at}")
    print(f"  finished           : {r.finished_at}")
    print(f"  pages crawled      : {r.pages_crawled}")
    print(f"  PDFs extracted     : {r.pdfs_extracted}")
    print(f"  pages failed       : {r.pages_failed}")
    print(f"  needed JS render   : {r.needed_js}")
    print(f"  unique URLs found  : {r.unique_urls_discovered}")
    print(f"  URLs from sitemaps : {r.urls_from_sitemaps}")
    print(f"  low-value skipped  : {r.skipped_low_value}")
    print(f"  PDFs skipped (>{config.MAX_PDF_SIZE_MB}MB): {r.pdfs_skipped_too_large}")
    print(f"  PDFs skipped (cap) : {r.pdfs_skipped_cap}")
    print(f"  page cap hit       : {r.page_cap_hit}  (cap={config.MAX_PAGES})")
    print(f"  pdf cap hit        : {r.pdf_cap_hit}  (cap={config.MAX_PDFS})")
    print(f"  distinct EXTERNAL domains encountered: {len(r.external_domains)}")
    top = list(r.external_domains.items())[:20]
    for dom, cnt in top:
        print(f"      - {dom}: {cnt} link(s)")
    print("=" * 64 + "\n")


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else config.BASE_URL
    crawl_site(base)
