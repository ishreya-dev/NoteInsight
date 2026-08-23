"""Unit tests for the lexical near-duplicate similarity utilities."""
import pytest

from app.services.similarity import (
    DEFAULT_NUM_BANDS,
    MinHash,
    build_shingles,
    compute_buckets,
    jaccard,
    lsh_buckets,
    lexical_similarity,
    normalize_text,
    text_similarity,
    tokenize,
)

_SAMPLE = (
    "Patient is a 64 year old male with type 2 diabetes mellitus, "
    "hypertension, and hyperlipidemia, currently managed on metformin, "
    "lisinopril, and atorvastatin with adequate glycemic control."
)

_SAMPLE_ONE_WORD = (
    "Patient is a 64 year old female with type 2 diabetes mellitus, "
    "hypertension, and hyperlipidemia, currently managed on metformin, "
    "lisinopril, and atorvastatin with adequate glycemic control."
)

_DIFFERENT = (
    "Pediatric patient presents with acute otitis media and a persistent "
    "fever for three days, treated with amoxicillin and supportive care."
)


def test_identical_text_has_unity_similarity_and_same_buckets():
    assert text_similarity(_SAMPLE, _SAMPLE) == 1.0
    assert compute_buckets(_SAMPLE) == compute_buckets(_SAMPLE)


def test_one_word_changed_is_high_similarity_and_shares_buckets():
    sim = text_similarity(_SAMPLE, _SAMPLE_ONE_WORD)
    assert sim == pytest.approx(0.78, abs=0.01)
    shared = set(compute_buckets(_SAMPLE)) & set(compute_buckets(_SAMPLE_ONE_WORD))
    assert len(shared) > 0


def test_whitespace_and_case_differences_are_ignored():
    messy = "  Patient   IS a 64 YEAR old male\nwith TYPE 2 diabetes  "
    canonical = "patient is a 64 year old male with type 2 diabetes"
    assert normalize_text(messy) == canonical
    assert compute_buckets(messy) == compute_buckets(canonical)
    assert text_similarity(messy, canonical) == 1.0


def test_clearly_different_text_has_low_similarity():
    assert text_similarity(_SAMPLE, _DIFFERENT) == 0.0


def test_bucket_generation_is_deterministic():
    first = compute_buckets(_SAMPLE)
    second = compute_buckets(_SAMPLE)
    assert first == second
    assert len(first) == DEFAULT_NUM_BANDS
    assert all(isinstance(b, str) and b.startswith("lsh:") for b in first)


def test_jaccard_of_equal_sets_is_unity_and_disjoint_is_zero():
    a = build_shingles(tokenize(_SAMPLE))
    assert jaccard(a, a) == 1.0
    b = build_shingles(tokenize(_DIFFERENT))
    assert jaccard(a, b) == 0.0


def test_minhash_signature_is_stable_and_length_matches():
    shingles = build_shingles(tokenize(_SAMPLE))
    sig_a = MinHash().signature(shingles)
    sig_b = MinHash().signature(shingles)
    assert sig_a == sig_b
    assert len(sig_a) == 128
    assert lsh_buckets(sig_a) == lsh_buckets(sig_b)


def test_lexical_similarity_identical_notes_is_unity():
    shingles = list(build_shingles(tokenize(_SAMPLE)))
    assert lexical_similarity(_SAMPLE, shingles) == 1.0


def test_lexical_similarity_near_identical_is_high():
    shingles = list(build_shingles(tokenize(_SAMPLE)))
    assert lexical_similarity(_SAMPLE_ONE_WORD, shingles) == pytest.approx(0.78, abs=0.01)


def test_lexical_similarity_clearly_different_is_low():
    shingles = list(build_shingles(tokenize(_SAMPLE)))
    assert lexical_similarity(_DIFFERENT, shingles) == 0.0


def test_lexical_similarity_missing_or_empty_shingles_is_zero():
    shingles = list(build_shingles(tokenize(_SAMPLE)))
    assert lexical_similarity(_SAMPLE, None) == 0.0
    assert lexical_similarity(_SAMPLE, []) == 0.0
    assert lexical_similarity("", shingles) == 0.0
