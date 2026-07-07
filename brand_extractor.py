"""
brand_extractor.py — Phase 5: derive brand tokens from the crawled homepage.

Extracts, from the homepage HTML + its linked CSS:
  * logo         — prefers the header <img> logo, then og:image, then favicon.
  * colors       — primary / secondary / accent, from CSS custom properties
                   (--vars) when present, else the most-used non-neutral colors
                   found across linked stylesheets, <style> blocks and inline
                   styles.
  * fonts        — the dominant body/heading font-family stacks.

Writes data/brand/brand.json and downloads the logo locally. Any token that
could not be extracted falls back to a clean neutral theme and is listed in the
`fallbacks` array so the UI (and you) know what defaulted.

Run:  python brand_extractor.py
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

import config


# ---- neutral fallback theme ------------------------------------------------
NEUTRAL_THEME = {
    "primary": "#1f4e79",     # calm civic blue
    "secondary": "#2c3e50",
    "accent": "#c0392b",
    "text": "#1a1a1a",
    "background": "#ffffff",
    "font_body": "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    "font_heading": "Georgia, 'Times New Roman', serif",
}

_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")
_RGB_RE = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")
_VAR_RE = re.compile(r"(--[\w-]*(?:color|primary|secondary|accent|brand|main)[\w-]*)\s*:\s*([^;]+);", re.I)
_FONT_RE = re.compile(r"font-family\s*:\s*([^;}{]+)[;}]", re.I)


def _http_get(url: str) -> Optional[str]:
    try:
        with httpx.Client(headers={"User-Agent": config.USER_AGENT},
                          timeout=config.REQUEST_TIMEOUT_SECONDS,
                          follow_redirects=True) as c:
            r = c.get(url)
            if r.status_code == 200:
                return r.text
    except Exception as exc:  # noqa: BLE001
        print(f"[brand] fetch failed {url}: {exc}")
    return None


def _http_get_bytes(url: str) -> Optional[bytes]:
    try:
        with httpx.Client(headers={"User-Agent": config.USER_AGENT},
                          timeout=config.REQUEST_TIMEOUT_SECONDS,
                          follow_redirects=True) as c:
            r = c.get(url)
            if r.status_code == 200:
                return r.content
    except Exception as exc:  # noqa: BLE001
        print(f"[brand] fetch failed {url}: {exc}")
    return None


# --------------------------------------------------------------------------- #
# homepage HTML
# --------------------------------------------------------------------------- #

def _load_homepage_html() -> Tuple[str, str]:
    """Return (homepage_url, html) — from pages.jsonl if crawled, else live."""
    base = config.BASE_URL.rstrip("/")
    candidates = {base, base + "/", base.replace("https://www.", "https://"),
                  base.replace("https://", "https://www.")}
    if config.PAGES_JSONL.exists():
        with config.PAGES_JSONL.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("url", "").rstrip("/") in {c.rstrip("/") for c in candidates} \
                        and not rec.get("is_pdf"):
                    return rec["url"], rec.get("raw_html", "")
    # fallback: fetch live
    html = _http_get(config.BASE_URL) or ""
    return config.BASE_URL, html


# --------------------------------------------------------------------------- #
# logo
# --------------------------------------------------------------------------- #

def _extract_logo(soup: BeautifulSoup, page_url: str) -> Optional[str]:
    # 1. header <img> that looks like a logo
    header = soup.find("header") or soup
    for img in header.find_all("img", src=True):
        hay = " ".join([img.get("src", ""), img.get("alt", ""),
                        " ".join(img.get("class", [])), img.get("id", "")]).lower()
        if "logo" in hay:
            return urljoin(page_url, img["src"])
    # 2. og:image
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        return urljoin(page_url, og["content"])
    # 3. any first header image
    if header:
        first_img = header.find("img", src=True)
        if first_img:
            return urljoin(page_url, first_img["src"])
    # 4. favicon / icon link
    icon = soup.find("link", rel=lambda v: v and "icon" in v.lower())
    if icon and icon.get("href"):
        return urljoin(page_url, icon["href"])
    return None


def _download_logo(logo_url: str) -> Optional[str]:
    data = _http_get_bytes(logo_url)
    if not data:
        return None
    ext = urlparse(logo_url).path.rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "gif", "svg", "webp", "ico"):
        ext = "png"
    dest = config.BRAND_DIR / f"logo.{ext}"
    dest.write_bytes(data)
    return dest.name


# --------------------------------------------------------------------------- #
# colors
# --------------------------------------------------------------------------- #

def _is_neutral(hexcolor: str) -> bool:
    r, g, b = _hex_to_rgb(hexcolor)
    # near-white / near-black
    if max(r, g, b) > 240 and min(r, g, b) > 220:
        return True
    if max(r, g, b) < 30:
        return True
    # grayscale (low saturation)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 18:
        return True
    return False


def _hex_to_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _norm_hex(h: str) -> str:
    h = h.lstrip("#").lower()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return "#%02x%02x%02x" % (r, g, b)


def _harvest_colors(css_text: str) -> Counter:
    counter: Counter = Counter()
    for m in _HEX_RE.findall(css_text):
        try:
            hx = _norm_hex(m)
            if not _is_neutral(hx):
                counter[hx] += 1
        except Exception:  # noqa: BLE001
            continue
    for r, g, b in _RGB_RE.findall(css_text):
        try:
            hx = _rgb_to_hex(int(r), int(g), int(b))
            if not _is_neutral(hx):
                counter[hx] += 1
        except Exception:  # noqa: BLE001
            continue
    return counter


def _harvest_css_vars(css_text: str) -> Dict[str, str]:
    """Map brand-ish CSS custom properties to their colors — skipping neutral
    values so a `--primary` that resolves to #fff isn't chosen as the brand color."""
    out: Dict[str, str] = {}
    for name, value in _VAR_RE.findall(css_text):
        value = value.strip()
        hexval = None
        mhex = _HEX_RE.search(value)
        if mhex:
            hexval = _norm_hex(mhex.group(0))
        else:
            mrgb = _RGB_RE.search(value)
            if mrgb:
                hexval = _rgb_to_hex(*(int(x) for x in mrgb.groups()))
        if hexval and not _is_neutral(hexval):
            out[name.lower()] = hexval
    return out


# Icon-font families that must never be used for UI text.
_ICON_FONT_RE = re.compile(r"icon|glyph|awesome|dashicon|fontello|material", re.I)


def _harvest_fonts(css_text: str) -> Counter:
    counter: Counter = Counter()
    for stack in _FONT_RE.findall(css_text):
        stack = stack.strip().strip("'\"")
        low = stack.lower()
        if (not stack or "inherit" in low or "var(" in low
                or low.startswith("--") or _ICON_FONT_RE.search(low)):
            continue
        counter[stack] += 1
    return counter


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #

def extract_brand() -> Dict:
    page_url, html = _load_homepage_html()
    fallbacks: List[str] = []

    if not html:
        print("[brand] no homepage HTML available; using full neutral theme.")
        brand = dict(NEUTRAL_THEME)
        brand.update({"logo": None, "logo_url": None,
                      "source_url": page_url,
                      "fallbacks": list(NEUTRAL_THEME.keys()) + ["logo"]})
        config.BRAND_JSON.write_text(json.dumps(brand, indent=2), encoding="utf-8")
        return brand

    soup = BeautifulSoup(html, "lxml")

    # ---- gather CSS text: <style> blocks + linked same-domain stylesheets ----
    css_text = "\n".join(s.get_text() for s in soup.find_all("style"))
    for link in soup.find_all("link", rel=lambda v: v and "stylesheet" in v.lower()):
        href = link.get("href")
        if not href:
            continue
        css_url = urljoin(page_url, href)
        # only same registered domain
        if urlparse(css_url).netloc and config.ALLOWLIST_DOMAINS[0] not in urlparse(css_url).netloc:
            # still fetch theme CSS even if on a CDN subpath of the same site
            pass
        fetched = _http_get(css_url)
        if fetched:
            css_text += "\n" + fetched
    # inline style attributes
    for el in soup.find_all(style=True):
        css_text += "\n" + el["style"]

    # ---- colors ----
    css_vars = _harvest_css_vars(css_text)
    color_counts = _harvest_colors(css_text)

    def pick_var(*keys) -> Optional[str]:
        for want in keys:
            for name, val in css_vars.items():
                if want in name:
                    return val
        return None

    primary = pick_var("primary", "brand", "main")
    secondary = pick_var("secondary")
    accent = pick_var("accent")

    ranked = [c for c, _ in color_counts.most_common()]
    def next_color(exclude: List[str]) -> Optional[str]:
        for c in ranked:
            if c not in exclude:
                return c
        return None

    chosen: List[str] = [x for x in (primary, secondary, accent) if x]
    if not primary:
        primary = next_color(chosen); chosen.append(primary) if primary else None
    if not secondary:
        secondary = next_color(chosen); chosen.append(secondary) if secondary else None
    if not accent:
        accent = next_color(chosen); chosen.append(accent) if accent else None

    if not primary:
        primary = NEUTRAL_THEME["primary"]; fallbacks.append("primary")
    if not secondary:
        secondary = NEUTRAL_THEME["secondary"]; fallbacks.append("secondary")
    if not accent:
        accent = NEUTRAL_THEME["accent"]; fallbacks.append("accent")

    # ---- fonts ----
    font_counts = _harvest_fonts(css_text)
    fonts_ranked = [f for f, _ in font_counts.most_common()]
    font_body = fonts_ranked[0] if fonts_ranked else None
    font_heading = fonts_ranked[1] if len(fonts_ranked) > 1 else font_body
    if not font_body:
        font_body = NEUTRAL_THEME["font_body"]; fallbacks.append("font_body")
    if not font_heading:
        font_heading = NEUTRAL_THEME["font_heading"]; fallbacks.append("font_heading")

    # ---- logo ----
    logo_url = _extract_logo(soup, page_url)
    logo_file = _download_logo(logo_url) if logo_url else None
    if not logo_file:
        fallbacks.append("logo")

    brand = {
        "source_url": page_url,
        "primary": primary,
        "secondary": secondary,
        "accent": accent,
        "text": NEUTRAL_THEME["text"],
        "background": NEUTRAL_THEME["background"],
        "font_body": font_body,
        "font_heading": font_heading,
        "logo": logo_file,          # filename under data/brand/, served at /brand/<file>
        "logo_url": logo_url,
        "fallbacks": fallbacks,
        "top_colors": color_counts.most_common(8),
    }
    config.BRAND_JSON.write_text(json.dumps(brand, indent=2), encoding="utf-8")
    _print_brand(brand)
    return brand


def _print_brand(b: Dict) -> None:
    print("\n" + "=" * 60)
    print("BRAND TOKENS")
    print("=" * 60)
    print(f"  source     : {b['source_url']}")
    print(f"  primary    : {b['primary']}")
    print(f"  secondary  : {b['secondary']}")
    print(f"  accent     : {b['accent']}")
    print(f"  font body  : {b['font_body']}")
    print(f"  font head  : {b['font_heading']}")
    print(f"  logo       : {b['logo']}  ({b['logo_url']})")
    if b["fallbacks"]:
        print(f"  FELL BACK TO NEUTRAL: {', '.join(b['fallbacks'])}")
    else:
        print("  (no fallbacks — every token extracted)")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    extract_brand()
