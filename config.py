"""
config.py — Central configuration for the Cameron County RAG chatbot.

Every tunable knob lives here so the whole pipeline (crawl -> clean -> index ->
serve) can be adjusted from one place. Values are chosen for a first, safe run
against https://www.cameroncountytx.gov/ and are documented inline.

Environment overrides: any value below can be overridden with an environment
variable of the same name (e.g. CRAWL_MAX_PAGES=500). See `_env_*` helpers.
"""
from __future__ import annotations

import os
from pathlib import Path

# Load a local .env file (if present) so ANTHROPIC_API_KEY and any overrides can
# live in a gitignored file instead of the shell environment. Never hardcode keys.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:  # noqa: BLE001 - dotenv is optional
    pass

# --------------------------------------------------------------------------- #
# Small helpers to read typed values from the environment (optional overrides)
# --------------------------------------------------------------------------- #

def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(__file__).resolve().parent
# DATA_DIR holds all generated artifacts (crawl output, vector index, brand).
# Override with the DATA_DIR env var to point at a mounted persistent disk when
# deploying (e.g. DATA_DIR=/var/data on Render) so the index survives restarts.
DATA_DIR = Path(_env_str("DATA_DIR", str(PROJECT_ROOT / "data")))
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
CHROMA_DIR = DATA_DIR / "chroma"
BRAND_DIR = DATA_DIR / "brand"

PAGES_JSONL = RAW_DIR / "pages.jsonl"
FAILED_URLS_TXT = RAW_DIR / "failed_urls.txt"
CHUNKS_JSONL = PROCESSED_DIR / "chunks.jsonl"
CRAWL_REPORT_JSON = RAW_DIR / "crawl_report.json"
# Corpus vocabulary for fuzzy (typo-tolerant) search, written at index build time
# and read at query time. Lives next to the index so it ships with it on deploy.
VOCAB_JSON = CHROMA_DIR / "vocab.json"
BRAND_JSON = BRAND_DIR / "brand.json"

for _d in (RAW_DIR, PROCESSED_DIR, CHROMA_DIR, BRAND_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- #
# Site / crawl scope
# --------------------------------------------------------------------------- #

BASE_URL = _env_str("BASE_URL", "https://www.cameroncountytx.gov/")

# Registered domains we are allowed to crawl. `www.` and bare host are both
# treated as in-scope automatically (see crawler URL normalization). The county
# links out to many *separate* affiliated domains — those are NOT crawled by the
# main crawler unless explicitly promoted here, and every distinct external
# domain encountered is logged in the crawl report so a human can decide whether
# to add it.
#
# APPROVED external domains (added after review): official county services that
# live on separate hosts but are part of the county's offering. An entry may be
# either a REGISTERED DOMAIN (matches all its subdomains, e.g. "agrilife.org")
# or a FULL HOST (matches only that exact host — used to keep a broad public
# suffix tightly scoped, e.g. "websvr.co.cameron.tx.us" instead of all "tx.us").
#   * agrilife.org            — Texas A&M AgriLife Extension Service (Extension Office)
#   * idocket.com             — court-case tracking portal (JUSTICE menu)
#   * applicantpool.com       — county Job Opportunities portal
#   * cameroncountytax.org    — Tax Office / Taxes Portal
#   * go2gov.net              — Pay Taxes search/payment portal
#   * kofilequicklinks.com    — Official Records Search + Clerk historical books
#   * tylerhost.net           — EnerGov Permits & Development / DOT Self Service
#   * websvr.co.cameron.tx.us — Jury Duty (host-scoped; NOT all of tx.us)
# These are indexed via the bounded, same-host `add_external_pages.py` helper
# (not an unbounded full-domain crawl).
ALLOWLIST_DOMAINS = [
    d.strip().lower()
    for d in _env_str(
        "ALLOWLIST_DOMAINS",
        "cameroncountytx.gov,agrilife.org,idocket.com,"
        "applicantpool.com,cameroncountytax.org,go2gov.net,"
        "kofilequicklinks.com,tylerhost.net,websvr.co.cameron.tx.us",
    ).split(",")
    if d.strip()
]

# Sitemaps to probe first (WordPress/Yoast + WP core defaults). Sitemap index
# files are followed to their child sitemaps automatically.
SITEMAP_CANDIDATES = [
    "/sitemap_index.xml",   # Yoast (declared in robots.txt for this site)
    "/sitemap.xml",
    "/wp-sitemap.xml",      # WordPress core default
]

USER_AGENT = _env_str(
    "USER_AGENT",
    "CameronCountyRAGBot/1.0 (+website-grounded QA assistant; contact site admin)",
)

# --------------------------------------------------------------------------- #
# Crawl behaviour / limits
# --------------------------------------------------------------------------- #

REQUESTS_PER_SECOND = _env_float("REQUESTS_PER_SECOND", 2.0)   # hard rate limit
REQUEST_TIMEOUT_SECONDS = _env_float("REQUEST_TIMEOUT_SECONDS", 30.0)
MAX_RETRIES = _env_int("MAX_RETRIES", 3)
RETRY_BACKOFF_BASE = _env_float("RETRY_BACKOFF_BASE", 1.5)     # exponential base
RESPECT_ROBOTS_TXT = _env_bool("RESPECT_ROBOTS_TXT", True)

# First-run caps (all configurable). Report notes if a cap was hit.
MAX_PAGES = _env_int("MAX_PAGES", 2000)          # total HTML pages
MAX_PDFS = _env_int("MAX_PDFS", 500)             # total PDFs whose text we extract
MAX_PDF_SIZE_MB = _env_float("MAX_PDF_SIZE_MB", 10.0)   # skip PDFs larger than this
MAX_DEPTH = _env_int("MAX_DEPTH", 20)            # BFS depth safety bound

# JS-render detection: if a fetched page yields < this many chars of visible
# text but the raw HTML is at least JS_RENDER_MIN_HTML bytes, retry with
# Playwright (if its browser is installed; otherwise the page is logged & kept).
JS_RENDER_MIN_TEXT_CHARS = _env_int("JS_RENDER_MIN_TEXT_CHARS", 200)
JS_RENDER_MIN_HTML_BYTES = _env_int("JS_RENDER_MIN_HTML_BYTES", 5000)
ENABLE_PLAYWRIGHT = _env_bool("ENABLE_PLAYWRIGHT", True)

# Also render when a page's visible text is a very small fraction of its HTML —
# the signature of JS-assembled content (menus, button/link grids, tabbed
# widgets) that the static fetch misses even when the page has some chrome text.
# The absolute-char trigger above alone never fires on such pages (they carry
# enough nav/footer text to clear 200 chars). NOTE: on heavy WordPress/TheGem
# builds almost every page has a low ratio, so a strict gate flags most of the
# site; tune per-site. Set RENDER_ALL_HTML for guaranteed (but slower) coverage.
JS_RENDER_MAX_TEXT_HTML_RATIO = _env_float("JS_RENDER_MAX_TEXT_HTML_RATIO", 0.012)
RENDER_ALL_HTML = _env_bool("RENDER_ALL_HTML", False)

# Non-HTML asset extensions we skip entirely (except PDFs, handled separately).
SKIP_ASSET_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tiff",
    ".mp4", ".mov", ".avi", ".wmv", ".mkv", ".webm", ".mp3", ".wav", ".ogg",
    ".zip", ".gz", ".tar", ".rar", ".7z",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",  # not parsed in v1
    ".css", ".js", ".json", ".xml", ".rss", ".woff", ".woff2", ".ttf", ".eot",
}

# URL substrings/patterns that generate infinite or low-value pages — skipped.
# (Calendar/date pagination, comment-reply links, and on-site search results.)
SKIP_URL_SUBSTRINGS = [
    "replytocom=",
    "?share=",
    "/wp-json/",
    "/wp-login",
    "/wp-admin",
    "/xmlrpc.php",
    "/feed/",
    "?s=",            # WordPress search results
    "/search/",
    "action=",        # calendar/event action links
    "ical=",          # iCal export
    "outlook-ical",
    "/events/?",      # event calendar date navigation
    "eventDisplay=",
    "tribe-bar-date=",
    "?filter",
]

# Regexes (as raw strings) for date/pagination traps and archive pagination.
# Applied in crawler.should_skip_url. Kept here so scope is fully configurable.
SKIP_URL_REGEXES = [
    r"/\d{4}/\d{2}/\d{2}/",              # /YYYY/MM/DD/ calendar day pages
    r"/\d{4}-\d{2}-\d{2}",               # ...-YYYY-MM-DD date suffixes
    r"[?&](?:tribe-bar-date|eventDate)=",
]

# Tag/category (and author) archive pagination beyond this page number is
# skipped (e.g. /category/news/page/4/ and higher).
MAX_ARCHIVE_PAGE = _env_int("MAX_ARCHIVE_PAGE", 3)

# Prioritize department/service pages over deep agenda/minutes archives. URLs
# whose path contains any of these are pushed to the FRONT of the BFS queue so
# they are crawled before the per-page cap is exhausted by archive pages.
PRIORITY_PATH_HINTS = [
    "department", "service", "office", "contact", "how-to", "apply",
    "permit", "tax", "vehicle", "records", "court", "election", "voter",
    "health", "road", "bridge", "park", "commissioner", "county-judge",
    "faq", "hours", "fees", "about",
]

# Path hints that indicate deep archive pages we DE-prioritize (crawled last).
DEPRIORITY_PATH_HINTS = [
    "agenda", "minutes", "/20", "archive", "/tag/", "/category/", "/author/",
    "notice", "budget-archive",
]

# --------------------------------------------------------------------------- #
# Cleaning / chunking (Phase 2)
# --------------------------------------------------------------------------- #

# bge-small-en-v1.5 has a 512-token max sequence length; anything longer is
# silently truncated at embed time. We therefore cap chunk size below that
# ceiling (the requested "~700 tokens" would overflow the model). Measured with
# the embedding model's own tokenizer for accuracy.
CHUNK_MAX_TOKENS = _env_int("CHUNK_MAX_TOKENS", 480)
CHUNK_OVERLAP_TOKENS = _env_int("CHUNK_OVERLAP_TOKENS", 100)
CHUNK_MIN_TOKENS = _env_int("CHUNK_MIN_TOKENS", 25)   # drop junk chunks below this

# A text block repeated across more than this fraction of pages is treated as
# boilerplate (nav/footer) that trafilatura missed, and removed.
BOILERPLATE_DOC_FRACTION = _env_float("BOILERPLATE_DOC_FRACTION", 0.5)
BOILERPLATE_MIN_PAGES = _env_int("BOILERPLATE_MIN_PAGES", 5)  # need enough pages first

# --------------------------------------------------------------------------- #
# Embeddings + vector store (Phase 3)
# --------------------------------------------------------------------------- #

EMBEDDING_PROVIDER = _env_str("EMBEDDING_PROVIDER", "sentence-transformers")
EMBEDDING_MODEL = _env_str("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_BATCH_SIZE = _env_int("EMBEDDING_BATCH_SIZE", 64)
CHROMA_COLLECTION = _env_str("CHROMA_COLLECTION", "cameron_county")

# --------------------------------------------------------------------------- #
# Retrieval + RAG answer engine (Phase 4)
# --------------------------------------------------------------------------- #

RETRIEVAL_TOP_K = _env_int("RETRIEVAL_TOP_K", 8)

# Hybrid re-ranking: pull a larger dense candidate pool, then re-rank by
# (cosine similarity + keyword-overlap boost) so chunks that literally contain
# query terms (e.g. "hours") aren't crowded out by topically-similar pages.
# Dense-only retrieval buried some exact-answer chunks below near-duplicate
# topical pages; this recovers them without changing the embedding model.
RETRIEVAL_CANDIDATE_POOL = _env_int("RETRIEVAL_CANDIDATE_POOL", 40)
KEYWORD_BOOST_WEIGHT = _env_float("KEYWORD_BOOST_WEIGHT", 0.20)

# Lexical branch: alongside the dense pool, directly fetch chunks whose text
# contains each rare query term (via Chroma $contains). This rescues exact-answer
# chunks that dense ANN recall misses — e.g. a query like "portals" whose only
# distinctive term sits in a small/new chunk that the approximate HNSW pool of
# RETRIEVAL_CANDIDATE_POOL never reaches. Purely additive: it can only widen the
# candidate set before re-ranking, never displace a better dense hit.
LEXICAL_MATCHES_PER_TERM = _env_int("LEXICAL_MATCHES_PER_TERM", 5)

# Fuzzy (typo-tolerant) branch: for a distinctive query term that is NOT in the
# corpus vocabulary (i.e. likely a misspelling like "comissioner" or "sherif"),
# find the closest real vocabulary terms via edit distance and add them to the
# lexical branch + keyword boost. Purely additive, like the lexical branch: it
# only rescues typo'd proper nouns, never displaces a better exact/dense hit.
# The vocabulary is written next to the index at build time (vocab.json).
FUZZY_SEARCH_ENABLED = _env_bool("FUZZY_SEARCH_ENABLED", True)
FUZZY_MIN_SCORE = _env_int("FUZZY_MIN_SCORE", 82)          # rapidfuzz ratio 0-100
FUZZY_MAX_MATCHES_PER_TERM = _env_int("FUZZY_MAX_MATCHES_PER_TERM", 3)
FUZZY_MAX_TERMS = _env_int("FUZZY_MAX_TERMS", 4)            # cap typo'd terms/query
FUZZY_MIN_TERM_LEN = _env_int("FUZZY_MIN_TERM_LEN", 4)      # don't fuzz very short terms
FUZZY_VOCAB_MIN_COUNT = _env_int("FUZZY_VOCAB_MIN_COUNT", 2)  # ignore hapax noise in vocab

# Relevance gate: if the best cosine similarity is below this, we do NOT call the
# LLM and return the not-available message. Tuned for bge-small (0.35 start).
SIMILARITY_THRESHOLD = _env_float("SIMILARITY_THRESHOLD", 0.35)

NOT_AVAILABLE_MESSAGE = (
    "The information you're asking about is not available on this website."
)

ANTHROPIC_MODEL = _env_str("ANTHROPIC_MODEL", "claude-sonnet-4-6")
ANTHROPIC_MAX_TOKENS = _env_int("ANTHROPIC_MAX_TOKENS", 1500)
# Fast, cheap model for the follow-up question-rewriting step.
ANTHROPIC_REWRITE_MODEL = _env_str("ANTHROPIC_REWRITE_MODEL", "claude-haiku-4-5")
ANTHROPIC_REWRITE_MAX_TOKENS = _env_int("ANTHROPIC_REWRITE_MAX_TOKENS", 256)

CONVERSATION_MEMORY_TURNS = _env_int("CONVERSATION_MEMORY_TURNS", 6)

# --------------------------------------------------------------------------- #
# API server / input guards (Phase 6)
# --------------------------------------------------------------------------- #

MAX_QUESTION_CHARS = _env_int("MAX_QUESTION_CHARS", 1000)
RATE_LIMIT_PER_SESSION_PER_MIN = _env_int("RATE_LIMIT_PER_SESSION_PER_MIN", 20)
SERVER_HOST = _env_str("SERVER_HOST", "127.0.0.1")
SERVER_PORT = _env_int("SERVER_PORT", 8000)
