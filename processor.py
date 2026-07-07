"""
processor.py — Phase 2: clean + chunk crawled pages into citation-ready chunks.

Pipeline:
  1. Read data/raw/pages.jsonl.
  2. Extract main content per page with trafilatura (Markdown output, so the
     heading hierarchy h1 > h2 > h3 survives) — nav bars, footers, cookie
     banners and sidebars are stripped by trafilatura. PDFs (whose "raw_html"
     is already-extracted text) are handled directly.
  3. Cross-page boilerplate detection: any text block repeated across more than
     BOILERPLATE_DOC_FRACTION of pages (that trafilatura missed) is removed.
  4. Heading-aware chunking: split on heading boundaries first, then split any
     section over CHUNK_MAX_TOKENS into overlapping sub-chunks at sentence
     boundaries (never mid-sentence). A chunk never mixes two pages.
  5. Attach citation metadata to EVERY chunk, drop junk/duplicate chunks, and
     write data/processed/chunks.jsonl.

Chunk record schema:
    {chunk_id, source_url, page_title, heading_path, chunk_index,
     crawl_timestamp, text, token_count, is_pdf}
"""
from __future__ import annotations

import json
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import config
from text_utils import count_tokens, split_sentences, text_hash, normalize_for_hash


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #

@dataclass
class Block:
    """A contiguous run of body text under a specific heading path."""
    heading_path: str
    text: str


@dataclass
class PageDoc:
    url: str
    title: str
    crawl_timestamp: str
    is_pdf: bool
    blocks: List[Block] = field(default_factory=list)


@dataclass
class ProcessStats:
    pages_read: int = 0
    pages_with_content: int = 0
    pages_empty: int = 0
    total_chunks: int = 0
    dropped_short: int = 0
    dropped_nav: int = 0
    dropped_garbage: int = 0
    dropped_duplicate: int = 0
    dropped_boilerplate_blocks: int = 0
    boilerplate_patterns: int = 0
    total_tokens: int = 0

    @property
    def avg_tokens(self) -> float:
        return (self.total_tokens / self.total_chunks) if self.total_chunks else 0.0


# --------------------------------------------------------------------------- #
# Content extraction (HTML -> heading-aware blocks)
# --------------------------------------------------------------------------- #

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# Unrendered WordPress shortcodes that leak into extracted text.
_SHORTCODE_RE = re.compile(
    r"\[/?(?:wpforms|caption|gallery|embed|vc_[\w]+|et_pb_[\w]+|"
    r"contact-form-7|cq_vc_[\w]+|su_[\w]+)[^\]]*\]", re.I)


# Surgical: strip ONLY unambiguous semantic chrome tags. Class/id-based
# stripping is deliberately avoided — it is theme-fragile and was observed
# nuking real content wrapped in layout classes like "content-sidebar".
# Removing <nav> alone eliminates the primary menu on this site; <header> is
# kept because some themes put the page's H1 title inside an article <header>.
_CHROME_TAGS = ("nav", "footer", "script", "style", "noscript", "form")

# Whole-line chrome fragments trafilatura occasionally leaves (a11y skip links,
# menu toggles). Compared case-insensitively against a stripped line.
_CHROME_LINES = {
    "skip to main content", "skip to content", "primary menu", "main menu",
    "menu", "close", "toggle navigation", "search", "back to top",
}


def _prestrip_chrome(html: str) -> str:
    """Remove unambiguous chrome tags so trafilatura cannot fall back to the
    site menu on thin pages. Skips already-decomposed descendants."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(_CHROME_TAGS):
        if not getattr(tag, "decomposed", False):
            tag.decompose()
    return str(soup)


def extract_blocks_from_html(html: str, fallback_title: str) -> List[Block]:
    """Extract main content as Markdown and group paragraphs by heading path."""
    import trafilatura

    cleaned = _prestrip_chrome(html)
    md = trafilatura.extract(
        cleaned,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        include_images=False,
        include_links=False,
        favor_recall=True,
        no_fallback=False,
    )
    if not md or not md.strip():
        return []
    return _markdown_to_blocks(md, fallback_title)


def _markdown_to_blocks(md: str, fallback_title: str) -> List[Block]:
    """Walk Markdown lines, maintaining an h1>h2>h3... heading stack, and group
    body text into blocks keyed by the current heading path."""
    heading_stack: List[Tuple[int, str]] = []  # (level, text)
    blocks: List[Block] = []
    buf: List[str] = []

    def current_path() -> str:
        parts = [fallback_title] if not heading_stack else []
        parts += [h for _, h in heading_stack]
        # de-dupe consecutive identical labels, keep order
        cleaned: List[str] = []
        for p in parts:
            p = p.strip()
            if p and (not cleaned or cleaned[-1] != p):
                cleaned.append(p)
        return " > ".join(cleaned) if cleaned else fallback_title

    def flush():
        text = "\n".join(buf).strip()
        buf.clear()
        if text:
            blocks.append(Block(heading_path=current_path(), text=text))

    for raw_line in md.splitlines():
        line = raw_line.rstrip()
        m = _MD_HEADING_RE.match(line.strip())
        if m:
            flush()  # close out text under the previous heading
            level = len(m.group(1))
            htext = m.group(2).strip().strip("#").strip()
            # pop headings at same-or-deeper level, then push
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            if htext:
                heading_stack.append((level, htext))
            continue
        if line.strip():
            # drop residual chrome fragments (skip links, menu toggles)
            if line.strip().lower() in _CHROME_LINES:
                continue
            cleaned_line = _SHORTCODE_RE.sub("", line.strip()).strip()
            if cleaned_line:
                buf.append(cleaned_line)
        else:
            # blank line = paragraph boundary; keep accumulating in same block
            buf.append("")
    flush()
    return blocks


def extract_blocks_from_pdf_text(text: str, title: str) -> List[Block]:
    """PDFs have no reliable heading structure — treat the whole document as one
    block under its filename title; the chunker will token-split it."""
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    return [Block(heading_path=title, text=text)]


# --------------------------------------------------------------------------- #
# Boilerplate detection across pages
# --------------------------------------------------------------------------- #

def find_boilerplate_hashes(pages: List[PageDoc]) -> set[str]:
    """Return hashes of text blocks that appear on > BOILERPLATE_DOC_FRACTION of
    pages — repeated nav/footer/banner text that trafilatura left behind."""
    n_pages = len(pages)
    if n_pages < config.BOILERPLATE_MIN_PAGES:
        return set()
    # count DISTINCT pages each block-hash appears on
    doc_freq: Counter[str] = Counter()
    for page in pages:
        seen_here: set[str] = set()
        for block in page.blocks:
            # split into paragraph-sized units so a shared footer paragraph is
            # caught even when surrounding text differs
            for para in re.split(r"\n{1,}", block.text):
                para = para.strip()
                if len(para) < 20:  # ignore tiny fragments
                    continue
                h = text_hash(para)
                if h not in seen_here:
                    seen_here.add(h)
                    doc_freq[h] += 1
    threshold = max(config.BOILERPLATE_MIN_PAGES,
                    int(n_pages * config.BOILERPLATE_DOC_FRACTION))
    return {h for h, c in doc_freq.items() if c >= threshold}


def strip_boilerplate(page: PageDoc, boiler: set[str], stats: ProcessStats) -> None:
    if not boiler:
        return
    for block in page.blocks:
        kept: List[str] = []
        for para in re.split(r"(\n{1,})", block.text):
            if para.strip() and text_hash(para.strip()) in boiler:
                stats.dropped_boilerplate_blocks += 1
                continue
            kept.append(para)
        block.text = "".join(kept).strip()
    page.blocks = [b for b in page.blocks if b.text.strip()]


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #

_NAV_HINT_RE = re.compile(
    r"^(home|menu|search|skip to (?:main )?content|toggle|previous|next|"
    r"read more|click here|back to top|share this)\b", re.I)


_NAV_KEYWORDS = ("primary menu", "skip to main content", "skip to content",
                 "main navigation", "toggle navigation")


def _looks_like_nav(text: str) -> bool:
    """Heuristic for pure-navigation / link-list chunks that survived
    extraction (e.g. a site menu trafilatura grabbed on a thin page)."""
    stripped = text.strip()
    low = stripped.lower()
    words = stripped.split()
    n_sentences = len(re.findall(r"[.!?]", stripped))
    has_sentence = n_sentences > 0

    if not has_sentence and len(words) < 12:
        return True
    if _NAV_HINT_RE.match(stripped) and not has_sentence:
        return True
    # explicit menu markers anywhere in a low-sentence chunk
    if n_sentences <= 1 and any(k in low for k in _NAV_KEYWORDS):
        return True
    # multi-line link list: many short lines, almost no sentences
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if len(lines) >= 10 and n_sentences <= max(1, int(len(lines) * 0.08)):
        avg_len = sum(len(ln) for ln in lines) / len(lines)
        if avg_len < 45:
            return True
    return False


def _looks_like_garbage(text: str) -> bool:
    """Detect low-value text extracted from map/graphic PDFs — e.g. street-name
    labels rendered as spaced-out single letters ('L A S P R I M A S')."""
    tokens = text.split()
    if len(tokens) < 20:
        return False
    single = sum(1 for t in tokens if len(t) == 1)
    return (single / len(tokens)) > 0.40


def chunk_section(heading_path: str, text: str) -> List[str]:
    """Split a heading section into <= CHUNK_MAX_TOKENS chunks with
    CHUNK_OVERLAP_TOKENS sentence overlap, never splitting mid-sentence."""
    if count_tokens(text) <= config.CHUNK_MAX_TOKENS:
        return [text.strip()] if text.strip() else []

    sentences = split_sentences(text)
    chunks: List[str] = []
    cur: List[str] = []
    cur_tokens = 0

    for sent in sentences:
        st = count_tokens(sent)
        # a single monster sentence: hard-split by tokens as a last resort
        if st > config.CHUNK_MAX_TOKENS:
            if cur:
                chunks.append(" ".join(cur).strip())
                cur, cur_tokens = [], 0
            chunks.extend(_hard_token_split(sent))
            continue
        if cur_tokens + st > config.CHUNK_MAX_TOKENS and cur:
            chunks.append(" ".join(cur).strip())
            # start next chunk with overlap sentences from the tail
            overlap, otok = [], 0
            for s in reversed(cur):
                stk = count_tokens(s)
                if otok + stk > config.CHUNK_OVERLAP_TOKENS:
                    break
                overlap.insert(0, s)
                otok += stk
            cur, cur_tokens = list(overlap), otok
        cur.append(sent)
        cur_tokens += st

    if cur:
        chunks.append(" ".join(cur).strip())
    return [c for c in chunks if c.strip()]


def _hard_token_split(text: str) -> List[str]:
    """Fallback for a single over-long sentence — split on the tokenizer with
    overlap, at word boundaries."""
    words = text.split()
    out, cur = [], []
    for w in words:
        cur.append(w)
        if count_tokens(" ".join(cur)) >= config.CHUNK_MAX_TOKENS:
            out.append(" ".join(cur))
            # keep an overlap tail
            tail, ttok = [], 0
            for x in reversed(cur):
                ttok += count_tokens(x)
                if ttok > config.CHUNK_OVERLAP_TOKENS:
                    break
                tail.insert(0, x)
            cur = tail
    if cur:
        out.append(" ".join(cur))
    return out


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def load_pages() -> List[PageDoc]:
    pages: List[PageDoc] = []
    with config.PAGES_JSONL.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            is_pdf = rec.get("is_pdf", False)
            title = (rec.get("title") or "").strip() or rec.get("url", "")
            if is_pdf:
                blocks = extract_blocks_from_pdf_text(rec.get("raw_html", ""), title)
            else:
                blocks = extract_blocks_from_html(rec.get("raw_html", ""), title)
            pages.append(PageDoc(
                url=rec.get("url", ""),
                title=title,
                crawl_timestamp=rec.get("fetch_timestamp", ""),
                is_pdf=is_pdf,
                blocks=blocks,
            ))
    return pages


def process() -> ProcessStats:
    stats = ProcessStats()
    pages = load_pages()
    stats.pages_read = len(pages)
    print(f"[process] read {len(pages)} page records")

    boiler = find_boilerplate_hashes(pages)
    stats.boilerplate_patterns = len(boiler)
    print(f"[process] {len(boiler)} cross-page boilerplate patterns detected")

    seen_hashes: set[str] = set()
    out_fh = config.CHUNKS_JSONL.open("w", encoding="utf-8")

    for page in pages:
        strip_boilerplate(page, boiler, stats)
        if not page.blocks:
            stats.pages_empty += 1
            continue
        stats.pages_with_content += 1

        chunk_index = 0
        for block in page.blocks:
            for chunk_text in chunk_section(block.heading_path, block.text):
                tok = count_tokens(chunk_text)

                # ---- junk filters ----
                if tok < config.CHUNK_MIN_TOKENS:
                    stats.dropped_short += 1
                    continue
                if _looks_like_nav(chunk_text):
                    stats.dropped_nav += 1
                    continue
                if _looks_like_garbage(chunk_text):
                    stats.dropped_garbage += 1
                    continue
                h = text_hash(chunk_text)
                if h in seen_hashes:
                    stats.dropped_duplicate += 1
                    continue
                seen_hashes.add(h)

                record = {
                    "chunk_id": str(uuid.uuid4()),
                    "source_url": page.url,
                    "page_title": page.title,
                    "heading_path": block.heading_path,
                    "chunk_index": chunk_index,
                    "crawl_timestamp": page.crawl_timestamp,
                    "text": chunk_text,
                    "token_count": tok,
                    "is_pdf": page.is_pdf,
                }
                out_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                stats.total_chunks += 1
                stats.total_tokens += tok
                chunk_index += 1

    out_fh.close()
    _print_stats(stats)
    return stats


def _print_stats(s: ProcessStats) -> None:
    print("\n" + "=" * 64)
    print("PROCESSING REPORT")
    print("=" * 64)
    print(f"  pages read              : {s.pages_read}")
    print(f"  pages with content      : {s.pages_with_content}")
    print(f"  pages empty (no content): {s.pages_empty}")
    print(f"  boilerplate patterns    : {s.boilerplate_patterns}")
    print(f"  boilerplate blocks cut  : {s.dropped_boilerplate_blocks}")
    print(f"  TOTAL CHUNKS            : {s.total_chunks}")
    print(f"  avg chunk size (tokens) : {s.avg_tokens:.1f}")
    print("  chunks dropped:")
    print(f"      too short (<{config.CHUNK_MIN_TOKENS} tok) : {s.dropped_short}")
    print(f"      nav-like text          : {s.dropped_nav}")
    print(f"      garbage (map/graphic)  : {s.dropped_garbage}")
    print(f"      duplicate (hash)       : {s.dropped_duplicate}")
    print(f"  output -> {config.CHUNKS_JSONL}")
    print("=" * 64 + "\n")


if __name__ == "__main__":
    process()
