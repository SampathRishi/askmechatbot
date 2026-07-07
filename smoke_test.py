"""
smoke_test.py — manual QA of the RAG answer engine (Phase 4 verification).

Runs the four required scenarios and writes a readable transcript:
  (a) a question the site clearly answers      -> grounded + citations
  (b) a question the site cannot answer         -> exact not-available message
  (c) a follow-up using a pronoun               -> rewritten + answered
  (d) a partially-covered question              -> partial answer + gap stated

Usage:  python smoke_test.py
"""
from __future__ import annotations

import json
import config
from rag import answer

OUT = config.PROCESSED_DIR / "_phase4_results.txt"


def show(lines, title, result, extra=""):
    lines.append("#" * 78)
    lines.append(title)
    if extra:
        lines.append(extra)
    lines.append(f"grounded : {result['grounded']}")
    lines.append("answer   :")
    lines.append(result["answer"])
    lines.append("citations:")
    if not result["citations"]:
        lines.append("   (none)")
    for c in result["citations"]:
        lines.append(f"   [{c['n']}] {c['page_title']} | {c['heading_path']}")
        lines.append(f"        {c['source_url']}")
    lines.append("")


def main():
    lines = []

    # (a) clearly answerable
    r = answer("What are the animal adoption hours at the Cameron County animal shelter?")
    show(lines, "(a) CLEARLY ANSWERABLE — animal shelter adoption hours", r)

    # (b) not answerable (off-topic; should trip the relevance gate)
    r = answer("What is the current price of Bitcoin in US dollars?")
    show(lines, "(b) NOT ANSWERABLE — Bitcoin price (off-topic)", r)

    # (c) follow-up with a pronoun, using conversation history
    history = [
        {"role": "user", "content": "Tell me about the Cameron County animal shelter."},
        {"role": "assistant",
         "content": "The Cameron County Animal Shelter runs an Animal Adoption "
                    "Program and handles animal control for the county."},
    ]
    r = answer("What are its adoption hours?", conversation_history=history)
    show(lines, "(c) FOLLOW-UP PRONOUN — 'What are its adoption hours?'", r,
         extra="(history establishes 'the Cameron County animal shelter')")

    # (d) partially covered — hours are on the site, adoption *fees* likely are not
    r = answer("What are the animal shelter adoption hours and how much does an adoption cost?")
    show(lines, "(d) PARTIALLY COVERED — adoption hours (present) + fees (maybe absent)", r)

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
