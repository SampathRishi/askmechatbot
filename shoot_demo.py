"""Screenshot the offline demo replica + chatbot widget."""
from playwright.sync_api import sync_playwright
import pathlib

OUT = pathlib.Path("data")
URL = "http://127.0.0.1:8080/index.html"

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
    pg.goto(URL, wait_until="networkidle", timeout=30000)
    pg.wait_for_timeout(1500)
    pg.wait_for_selector("#cc-chat-launcher", timeout=8000)
    # 1) top of page with launcher visible
    pg.screenshot(path=str(OUT / "demo_1_home.png"))
    print("shot 1 (home + launcher) done")
    # 2) open the chat panel
    pg.click("#cc-chat-launcher")
    pg.wait_for_selector("#cc-chat-panel.cc-open", timeout=5000)
    pg.wait_for_timeout(4500)  # let the embedded RAG app render inside the iframe
    pg.screenshot(path=str(OUT / "demo_2_open.png"))
    print("shot 2 (panel open) done")
    # 3) expand to full screen
    pg.click("#cc-chat-expand")
    pg.wait_for_selector("#cc-chat-panel.cc-full", timeout=5000)
    pg.wait_for_timeout(1500)
    pg.screenshot(path=str(OUT / "demo_3_full.png"))
    print("shot 3 (full screen) done")
    b.close()
