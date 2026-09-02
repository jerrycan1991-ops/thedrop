"""Cheap deduplication. No model, no embedding, no GPU (PIPELINE.md §4).

Three cascading checks, cheapest first:

1. **Exact URL** -- the unique constraint on `raw_articles.url_hash`. An insert
   conflict is the duplicate detection; it costs nothing and, being a database
   constraint, is correct even when two pollers race.
2. **Content hash** -- sha256 of the whitespace-collapsed body, catching identical
   syndication published under different URLs.
3. **SimHash** -- 64-bit over title + first 400 chars, near-duplicates at Hamming
   distance <= 3.

Anything surviving all three is `unique` and eligible for embedding on the desktop.
Semantic near-duplicates that survive SimHash are caught later at clustering, which is
the correct place for them -- this stage exists to keep obvious repeats off the desktop,
not to be clever.

The whole cascade runs in single-digit milliseconds, which is the entire reason ML
stays off the VPS.
"""

from __future__ import annotations

import hashlib
import re

#: Hamming distance at or below which two items are considered near-duplicates.
#: 3 of 64 bits. Lower misses reworded syndication; higher starts merging genuinely
#: different stories that share a topic and a wire lede.
NEAR_DUPLICATE_DISTANCE = 3

#: SimHash is computed over the title plus this much body. Enough to capture the lede,
#: where syndicated copies agree, without letting a long divergent tail dominate.
BODY_CHARS_FOR_SIMHASH = 400

_SIMHASH_BITS = 64
_MASK_64 = (1 << _SIMHASH_BITS) - 1

#: Bands for candidate lookup: 4 x 16 bits. Two items within Hamming distance 3 must
#: agree exactly on at least one band (pigeonhole: 3 differing bits cannot touch all 4).
BAND_COUNT = 4
BAND_BITS = _SIMHASH_BITS // BAND_COUNT

_TOKEN = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def simhash(title: str, body_text: str = "") -> int:
    """64-bit SimHash over title + the first 400 characters of body.

    Returned SIGNED, because Postgres has no unsigned bigint and the column is a
    plain `bigint`. Only Hamming distance is ever computed on it, and that is
    sign-agnostic once both operands are masked back to 64 bits -- so the wrap is safe
    as long as nobody compares these values by magnitude, which would be meaningless
    anyway.
    """
    tokens = _tokens(f"{title} {body_text[:BODY_CHARS_FOR_SIMHASH]}")
    if not tokens:
        return 0

    vector = [0] * _SIMHASH_BITS
    for token in tokens:
        # Shingling on single tokens loses word order, which is what we want: a
        # reordered lede is still the same story.
        digest = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
        for bit in range(_SIMHASH_BITS):
            vector[bit] += 1 if digest >> bit & 1 else -1

    unsigned = 0
    for bit in range(_SIMHASH_BITS):
        if vector[bit] > 0:
            unsigned |= 1 << bit

    return to_signed(unsigned)


def to_signed(unsigned: int) -> int:
    """Map a 64-bit unsigned value into the signed range Postgres bigint accepts."""
    unsigned &= _MASK_64
    return unsigned - (1 << _SIMHASH_BITS) if unsigned >> (_SIMHASH_BITS - 1) else unsigned


def to_unsigned(signed: int) -> int:
    return signed & _MASK_64


def hamming_distance(a: int, b: int) -> int:
    """Bits that differ. Operands may be signed or unsigned; both are masked first."""
    return ((a & _MASK_64) ^ (b & _MASK_64)).bit_count()


def bands(value: int) -> list[int]:
    """Split into BAND_COUNT equal bands for candidate lookup.

    Two values within NEAR_DUPLICATE_DISTANCE must share at least one band exactly, so
    a band match is a cheap prefilter that cannot produce a false negative while
    NEAR_DUPLICATE_DISTANCE < BAND_COUNT.
    """
    unsigned = to_unsigned(value)
    mask = (1 << BAND_BITS) - 1
    return [(unsigned >> (i * BAND_BITS)) & mask for i in range(BAND_COUNT)]


def is_near_duplicate(a: int, b: int, threshold: int = NEAR_DUPLICATE_DISTANCE) -> bool:
    return hamming_distance(a, b) <= threshold


def content_digest(body_text: str) -> bytes:
    """sha256 over whitespace-collapsed body -- check 2 of the cascade.

    Mirrors normalize.content_hash; kept here so the dedup module reads as a whole.
    """
    return hashlib.sha256(" ".join(body_text.split()).encode("utf-8")).digest()
