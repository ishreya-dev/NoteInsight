"""Safety helpers for the near-duplicate analysis cache.

Each helper checks a single safety property and returns a boolean. Higher-level
decisions (similarity thresholds, candidate ranking, cache selection) are
composed elsewhere and are intentionally not implemented here.

The meaningful-change detector is deliberately conservative: it errs toward
rejecting cache reuse. When it cannot compare notes (e.g. no cached shingle
set) or when the new note introduces important medical content that the cached
note did not contain, it reports a meaningful change.
"""

from __future__ import annotations

from typing import Any

from app.services.gemini_client import verify_evidence_quote
from app.services.similarity import lexical_similarity, tokenize

# Conservative, non-exhaustive seed lexicons. These intentionally favor
# rejection: a newly introduced term from any of these categories is treated
# as a meaningful clinical change. The lists can be expanded over time.
_MEDICATION_TERMS = frozenset(
    {
        "metformin", "insulin", "lisinopril", "atorvastatin", "simvastatin",
        "amlodipine", "losartan", "warfarin", "clopidogrel", "aspirin",
        "ibuprofen", "omeprazole", "prednisone", "albuterol", "levothyroxine",
        "gabapentin", "sertraline", "fluoxetine", "amoxicillin", "azithromycin",
        "furosemide", "spironolactone", "metoprolol", "carvedilol", "apixaban",
    }
)

_ALLERGY_TERMS = frozenset(
    {
        "allergy", "allergies", "allergic", "anaphylaxis", "penicillin",
        "cephalosporin", "sulfa", "latex", "peanut", "tree", "nut", "shellfish",
    }
)

_DIAGNOSIS_TERMS = frozenset(
    {
        "diabetes", "hypertension", "asthma", "copd", "chf", "afib", "stroke",
        "seizure", "cancer", "tumor", "infection", "pneumonia", "sepsis",
        "renal", "hepatic", "hyperlipidemia", "obesity", "depression",
        "anxiety", "hypothyroidism", "hyperthyroidism", "osteoporosis",
    }
)

_NEGATION_TERMS = frozenset(
    {
        "denies", "denied", "no", "not", "without", "negative", "resolved",
        "resolves", "stopped", "discontinued", "ceased", "refuses", "declines",
        "absent", "lacking", "rules", "contraindicated", "worsening", "worsened",
        "new", "developed", "onset", "developing",
    }
)

# Medically relevant demographic flips (e.g. sex, pregnancy) that can change
# the safety of reusing a cached analysis.
_DEMOGRAPHIC_TERMS = frozenset(
    {
        "male", "female", "pregnant", "pregnancy", "postmenopausal",
        "menopausal", "puberty", "neonate", "infant", "pediatric", "adolescent",
    }
)

_MEANINGFUL_TERMS = (
    _MEDICATION_TERMS
    | _ALLERGY_TERMS
    | _DIAGNOSIS_TERMS
    | _NEGATION_TERMS
    | _DEMOGRAPHIC_TERMS
)


def evidence_quotes_valid(candidate: dict[str, Any], note_text: str) -> bool:
    """Return True only if every cached evidence quote is valid for note_text.

    Reuses the existing ``verify_evidence_quote`` logic exactly. A cached
    condition without an evidence quote cannot be validated and is treated as
    invalid. When a candidate has no conditions, all (zero) quotes are
    trivially valid.
    """
    conditions = candidate.get("conditions") or []
    for condition in conditions:
        if not isinstance(condition, dict):
            return False
        quote = condition.get("evidence_quote")
        if not quote:
            return False
        if not verify_evidence_quote(quote, note_text):
            return False
    return True


def _tokens_from_shingles(shingles: list[str]) -> frozenset[str]:
    tokens: set[str] = set()
    for shingle in shingles:
        tokens.update(shingle.split())
    return frozenset(tokens)


def _numeric_tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in tokenize(text) if t.isdigit())


def has_meaningful_change(
    new_text: str,
    cached_shingles: list[str] | None,
) -> bool:
    """Return True if the new and cached notes differ by a meaningful change.

    Compares the new note against the cached note's stored shingle set. A
    meaningful change is reported when a medication, allergy, diagnosis/
    condition, negation, or relevant demographic term is present in one note
    but not the other (either direction: addition, removal, or replacement),
    or when the numeric values (e.g. dosage, measurement, age) differ between
    the two notes. When the cached shingle set is missing, the comparison
    cannot be made and the result is conservatively True.
    """
    if not cached_shingles:
        return True

    new_tokens = set(tokenize(new_text))
    cached_tokens = _tokens_from_shingles(cached_shingles)

    # Symmetric: a meaningful term appearing in exactly one of the notes
    # indicates an addition, removal, or replacement in either direction.
    if (new_tokens ^ cached_tokens) & _MEANINGFUL_TERMS:
        return True

    if _numeric_tokens(new_text) != {t for t in cached_tokens if t.isdigit()}:
        return True

    return False


# Very conservative default for reusing a near-duplicate cached analysis.
DEFAULT_SIMILARITY_THRESHOLD = 0.95


def select_best_similar_candidate(
    note_text: str,
    candidates: list[dict[str, Any]],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> dict[str, Any] | None:
    """Return the best safely-reusable cached candidate, or ``None``.

    For each candidate this recomputes the actual lexical (Jaccard) similarity
    over shingle sets, then enforces every safety check:
      * similarity must meet ``threshold`` (default 0.95),
      * every cached evidence quote must still be valid for the new note,
      * no meaningful medical change may exist between the notes.
    Candidates missing a shingle set are skipped (their similarity is 0.0).
    When multiple candidates pass, the one with the highest similarity wins.
    When none pass, ``None`` is returned so the caller can fall back to Gemini.
    """
    best_candidate: dict[str, Any] | None = None
    best_score = -1.0

    for candidate in candidates:
        shingles = candidate.get("shingles")
        score = lexical_similarity(note_text, shingles)
        if score < threshold:
            continue
        if not evidence_quotes_valid(candidate, note_text):
            continue
        if has_meaningful_change(note_text, shingles):
            continue
        if score > best_score:
            best_score = score
            best_candidate = candidate

    return best_candidate
