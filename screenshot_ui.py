"""Drive the running UI with Playwright and capture verification screenshots."""
from playwright.sync_api import sync_playwright
import config

URL = f"http://{config.SERVER_HOST}:{config.SERVER_PORT}/"
SHOT_DIR = config.DATA_DIR

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 900, "height": 1000},
                            device_scale_factor=2)
    page.goto(URL, wait_until="networkidle")
    page.wait_for_selector(".bubble-bot", timeout=15000)
    page.wait_for_timeout(1200)  # let brand fonts/logo settle
    page.screenshot(path=str(SHOT_DIR / "ui_1_initial.png"))
    print("shot 1 (initial) done")

    # ask a question
    page.fill(".input", "What are the animal shelter adoption hours, and how much does adoption cost?")
    page.click(".send")
    # wait for citation chips (only appear on a grounded answer)
    page.wait_for_selector(".chip", timeout=60000)
    page.wait_for_timeout(800)
    # open the first citation card
    page.click(".chip")
    page.wait_for_selector(".cite-card", timeout=5000)
    page.wait_for_timeout(500)
    page.screenshot(path=str(SHOT_DIR / "ui_2_answer.png"), full_page=True)
    print("shot 2 (answer + citation) done")

    browser.close()
