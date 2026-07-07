"""
text_utils.py — shared tokenization + sentence utilities.

Token counts are measured with the *embedding model's own tokenizer* so chunk
sizing matches exactly what the model will actually see at embed time (bge-small
truncates at 512 tokens). The tokenizer loads with `transformers` only — no
torch required — so Phase 2 can run without the heavy ML stack.
"""
from __future__ import annotations

import re
import hashlib
from functools import lru_cache
from typing import List

import config


@lru_cache(maxsize=1)
def _tokenizer():
    """Lazily load and cache the embedding model's tokenizer."""
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(config.EMBEDDING_MODEL)


def count_tokens(text: str) -> int:
    """Number of tokens `text` occupies for the embedding model."""
    if not text:
        return 0
    return len(_tokenizer().encode(text, add_special_tokens=False))


# Sentence splitter: split after . ! ? (and their quoted variants) when followed
# by whitespace + a capital/opening char. Deliberately conservative so we never
# split mid-sentence; abbreviations occasionally merge two sentences, which is
# harmless for chunking.
_SENTENCE_RE = re.compile(r"(?<=[.!?])[\"')\]]?\s+(?=[A-Z0-9(\"'\[])")


def split_sentences(text: str) -> List[str]:
    text = text.strip()
    if not text:
        return []
    # split on hard newlines first (preserves list items / paragraph breaks),
    # then sentence-split each line.
    out: List[str] = []
    for line in re.split(r"\n+", text):
        line = line.strip()
        if not line:
            continue
        pieces = _SENTENCE_RE.split(line)
        out.extend(p.strip() for p in pieces if p.strip())
    return out


def normalize_for_hash(text: str) -> str:
    """Lowercase + collapse whitespace, for duplicate detection."""
    return re.sub(r"\s+", " ", text.strip().lower())


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_for_hash(text).encode("utf-8")).hexdigest()
