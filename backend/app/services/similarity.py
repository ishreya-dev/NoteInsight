"""Lexical near-duplicate matching utilities.

Provides deterministic text normalization, Jaccard similarity, and MinHash/LSH
bucket generation for candidate lookup. This module is intentionally isolated
from Firestore and the existing exact cache; it only computes signatures.
"""

from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Iterable

_TOKEN_RE = re.compile(r"[a-z0-9]+")

DEFAULT_NUM_HASHES = 128
DEFAULT_NUM_BANDS = 32
DEFAULT_SHINGLE_SIZE = 3

_LARGE_PRIME = (1 << 61) - 1


def normalize_text(text: str) -> str:
    """Lowercase and collapse whitespace for order-insensitive comparison."""
    return re.sub(r"\s+", " ", text.strip().lower())


def tokenize(text: str) -> list[str]:
    """Split normalized text into lowercase alphanumeric word tokens."""
    return _TOKEN_RE.findall(normalize_text(text))


def build_shingles(
    tokens: list[str],
    k: int = DEFAULT_SHINGLE_SIZE,
) -> frozenset[str]:
    """Return the set of contiguous k-word shingles for the token list.

    Short token lists (fewer than ``k`` tokens) fall back to the individual
    tokens so that non-empty text still produces a non-empty shingle set.
    """
    if len(tokens) < k:
        return frozenset(tokens)
    return frozenset(" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1))


def jaccard(set_a: frozenset[str], set_b: frozenset[str]) -> float:
    """Return the Jaccard similarity (intersection over union) of two sets."""
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    if not union:
        return 1.0
    return len(set_a & set_b) / len(union)


def text_similarity(text_a: str, text_b: str, k: int = DEFAULT_SHINGLE_SIZE) -> float:
    """Return the shingle Jaccard similarity between two raw note texts."""
    return jaccard(build_shingles(tokenize(text_a), k), build_shingles(tokenize(text_b), k))


def _shingle_hash(shingle: str) -> int:
    digest = hashlib.sha256(shingle.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


class MinHash:
    """Deterministic MinHash signature over a shingle set.

    Permutations are derived from a fixed seed so signatures are stable
    across processes (unlike Python's built-in ``hash``).
    """

    def __init__(self, num_hashes: int = DEFAULT_NUM_HASHES, seed: int = 1) -> None:
        self.num_hashes = num_hashes
        rng = random.Random(seed)
        self._a = [rng.randrange(1, _LARGE_PRIME) for _ in range(num_hashes)]
        self._b = [rng.randrange(0, _LARGE_PRIME) for _ in range(num_hashes)]

    def signature(self, shingles: frozenset[str]) -> tuple[int, ...]:
        if not shingles:
            return tuple(_LARGE_PRIME for _ in range(self.num_hashes))
        result = []
        for i in range(self.num_hashes):
            a = self._a[i]
            b = self._b[i]
            result.append(
                min((a * _shingle_hash(s) + b) % _LARGE_PRIME for s in shingles)
            )
        return tuple(result)


def lsh_buckets(
    signature: tuple[int, ...],
    num_bands: int = DEFAULT_NUM_BANDS,
    prefix: str = "lsh",
) -> list[str]:
    """Split a MinHash signature into LSH band buckets for candidate lookup.

    Each band is hashed into a single bucket id. Notes sharing a bucket for
    any band are candidate near-duplicates.
    """
    n = len(signature)
    if n == 0:
        return []
    rows = max(1, n // num_bands)
    bands = num_bands if rows > 1 else n
    buckets: list[str] = []
    for band in range(bands):
        start = band * rows
        band_vals = signature[start : start + rows]
        band_hash = hashlib.sha256(
            "|".join(str(v) for v in band_vals).encode("utf-8")
        ).hexdigest()[:16]
        buckets.append(f"{prefix}:{band}:{band_hash}")
    return buckets


def compute_buckets(
    text: str,
    num_hashes: int = DEFAULT_NUM_HASHES,
    num_bands: int = DEFAULT_NUM_BANDS,
    k: int = DEFAULT_SHINGLE_SIZE,
    seed: int = 1,
) -> list[str]:
    """Compute the LSH lookup buckets for a raw note text."""
    shingles = build_shingles(tokenize(text), k)
    signature = MinHash(num_hashes=num_hashes, seed=seed).signature(shingles)
    return lsh_buckets(signature, num_bands=num_bands)


def lexical_similarity(
    text: str,
    candidate_shingles: Iterable[str] | None,
) -> float:
    """Return the exact lexical similarity between a note and a cached candidate.

    Computes the Jaccard similarity over the word-shingle sets (not the
    approximate MinHash signature). A candidate with missing or empty shingles
    cannot be compared and is treated as completely dissimilar (0.0).
    """
    if not candidate_shingles:
        return 0.0
    return jaccard(
        build_shingles(tokenize(text)),
        frozenset(candidate_shingles),
    )
