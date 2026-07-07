"""
pipeline.py — Phase 6: run the whole data pipeline end-to-end.

    python pipeline.py                # crawl -> clean -> index -> brand
    python pipeline.py --skip-crawl   # reuse existing pages.jsonl
    python pipeline.py --only index   # run a single stage

Stages: crawl, clean, index, brand. The RAG answer engine and web server read
the artifacts this pipeline produces; re-run this whenever the site changes.
"""
from __future__ import annotations

import argparse
import time

import config


STAGES = ["crawl", "clean", "index", "brand"]


def run_crawl() -> None:
    from crawler import crawl_site
    print("\n### STAGE: crawl ###")
    crawl_site(config.BASE_URL)


def run_clean() -> None:
    from processor import process
    print("\n### STAGE: clean/chunk ###")
    process()


def run_index() -> None:
    from indexer import build_index
    print("\n### STAGE: embed/index ###")
    build_index()


def run_brand() -> None:
    from brand_extractor import extract_brand
    print("\n### STAGE: brand extraction ###")
    extract_brand()


RUNNERS = {
    "crawl": run_crawl,
    "clean": run_clean,
    "index": run_index,
    "brand": run_brand,
}


def main() -> None:
    ap = argparse.ArgumentParser(description="Cameron County RAG data pipeline")
    ap.add_argument("--skip-crawl", action="store_true",
                    help="reuse existing data/raw/pages.jsonl")
    ap.add_argument("--only", choices=STAGES,
                    help="run only this single stage")
    args = ap.parse_args()

    if args.only:
        stages = [args.only]
    else:
        stages = [s for s in STAGES if not (args.skip_crawl and s == "crawl")]

    t0 = time.time()
    for stage in stages:
        s0 = time.time()
        RUNNERS[stage]()
        print(f"[pipeline] stage '{stage}' finished in {time.time() - s0:.1f}s")
    print(f"\n[pipeline] ALL DONE in {time.time() - t0:.1f}s. "
          f"Start the server with:  python server.py")


if __name__ == "__main__":
    main()
