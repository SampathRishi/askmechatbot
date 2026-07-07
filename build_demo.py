"""
build_demo.py — assemble a clean, self-contained offline replica of the
Cameron County homepage in demo-site/, with the chatbot widget injected.

  * copies the saved _files assets into demo-site/assets/
  * rewrites the saved-page asset paths (./...files/) to assets/
  * strips analytics / phone-home scripts (Google gtag, Siteimprove,
    speculation-rules prefetch, wp-emoji loader)
  * removes srcset (keeps the locally-saved src so images render offline)
  * downloads the few visible background images referenced by absolute URL
  * injects <script src="chatbot-widget.js"></script> before </body>
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
SRC_HTML = ROOT / "Cameron County Homepage - Cameron County.html"
SRC_FILES = ROOT / "Cameron County Homepage - Cameron County_files"
DEMO = ROOT / "demo-site"
ASSETS = DEMO / "assets"

FILES_PREFIXES = [
    "./Cameron County Homepage - Cameron County_files/",
    "Cameron County Homepage - Cameron County_files/",
    "./Cameron%20County%20Homepage%20-%20Cameron%20County_files/",
    "Cameron%20County%20Homepage%20-%20Cameron%20County_files/",
]

# script blocks that phone home / track / break offline — removed wholesale
TRACKER_PATTERNS = [
    r"<!-- Google tag \(gtag\.js\) -->",
    r"<script[^>]*googletagmanager[^>]*>\s*</script>",
    r"<script>\s*window\.dataLayer[\s\S]*?gtag\('config'[\s\S]*?</script>",
    r"<script[^>]*siteanalyze[^>]*>\s*</script>",
    r'<script type="speculationrules">[\s\S]*?</script>',
    r'<script id="wp-emoji-settings"[\s\S]*?</script>',
    r'<script type="module">[\s\S]*?wp-emoji[\s\S]*?</script>',
    r"<script[^>]*wp-emoji-release[^>]*>\s*</script>",
]

# absolute/protocol-relative image URLs on the county domain (visible backgrounds)
_IMG_URL_RE = re.compile(
    r"(?:https?:)?//www\.cameroncountytx\.gov/[^\s\"')]+?\.(?:png|jpe?g|gif|svg|webp)(?:\?[^\s\"')]*)?",
    re.I,
)


def main() -> None:
    if DEMO.exists():
        shutil.rmtree(DEMO)
    DEMO.mkdir(parents=True)
    shutil.copytree(SRC_FILES, ASSETS)
    print(f"[demo] copied {len(list(ASSETS.iterdir()))} assets -> {ASSETS}")

    html = SRC_HTML.read_text(encoding="utf-8", errors="replace")

    # 1. strip trackers / phone-home scripts
    removed = 0
    for pat in TRACKER_PATTERNS:
        html, n = re.subn(pat, "", html, flags=re.I)
        removed += n
    print(f"[demo] stripped {removed} tracker/phone-home blocks")

    # 2. drop srcset (rely on the locally-saved src)
    html, n_srcset = re.subn(r'\s+(?:data-)?srcset="[^"]*"', "", html)
    print(f"[demo] removed {n_srcset} srcset attributes")

    # 3. rewrite saved-asset folder refs -> assets/
    for pref in FILES_PREFIXES:
        html = html.replace(pref, "assets/")

    # 4. localize remaining absolute county image URLs (download if not saved)
    downloaded, failed, mapped = [], [], 0
    client = httpx.Client(timeout=30, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (demo build)"})

    def _localize(m: re.Match) -> str:
        nonlocal mapped
        url = m.group(0)
        base = url.split("?")[0].rsplit("/", 1)[-1]
        dest = ASSETS / base
        if dest.exists():
            mapped += 1
            return f"assets/{base}"
        full = ("https:" + url) if url.startswith("//") else url
        try:
            r = client.get(full)
            if r.status_code == 200 and r.content:
                dest.write_bytes(r.content)
                downloaded.append(base)
                return f"assets/{base}"
        except Exception as exc:  # noqa: BLE001
            failed.append((base, str(exc)))
        failed.append((base, "not downloaded"))
        return url  # leave as-is (fails silently offline)

    html = _IMG_URL_RE.sub(_localize, html)
    client.close()
    print(f"[demo] localized {mapped} already-saved image URLs; "
          f"downloaded {len(downloaded)} missing ({', '.join(downloaded) or 'none'})")
    if failed:
        uniq = sorted({b for b, _ in failed})
        print(f"[demo] {len(uniq)} image URLs left as-is: {', '.join(uniq)}")

    # 5. inject the chatbot widget (single embed tag — like a real customer)
    if "</body>" in html:
        html = html.replace(
            "</body>",
            '\n<!-- Cameron County chatbot widget (single-line embed) -->\n'
            '<script src="chatbot-widget.js"></script>\n</body>',
            1,
        )
    print("[demo] injected chatbot-widget.js embed tag")

    (DEMO / "index.html").write_text(html, encoding="utf-8")
    print(f"[demo] wrote {DEMO / 'index.html'} ({len(html)//1024} KB)")


if __name__ == "__main__":
    main()
