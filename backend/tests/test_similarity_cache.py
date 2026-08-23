"""Focused tests for the similarity-cache safety helpers."""
from app.services.similarity import build_shingles, tokenize
from app.services.similarity_cache import (
    DEFAULT_SIMILARITY_THRESHOLD,
    evidence_quotes_valid,
    has_meaningful_change,
    select_best_similar_candidate,
)


def _cached(text):
    return list(build_shingles(tokenize(text)))


# Long clinical note so a single-word change still exceeds the 0.95 threshold.
BASE = (
    "patient is a 64 year old male with type 2 diabetes mellitus hypertension "
    "and hyperlipidemia managed on metformin lisinopril and atorvastatin with "
    "adequate glycemic control and stable vitals no known drug allergies "
    "followed in clinic for routine care reports good adherence to medications "
    "and no tobacco history with regular exercise and balanced diet sleeps "
    "well and has no family history of cardiac disease ambulates without "
    "assistance and maintains healthy weight and attends annual wellness visits "
    "and denies chest pain and has normal renal function and normal liver "
    "function and no known malignancy and is up to date on vaccinations and is "
    "independent with activities of daily living and has stable body mass index "
    "and no history of stroke and no history of seizures and takes aspirin "
    "daily and monitors blood glucose weekly"
)
QUOTE = "type 2 diabetes mellitus"


def _candidate(note_text=BASE, quote=QUOTE, summary="s"):
    return {
        "shingles": list(build_shingles(tokenize(note_text))),
        "conditions": [
            {"condition_name": "Type 2 diabetes mellitus", "evidence_quote": quote}
        ],
        "summary": summary,
    }


_NOTE = (
    "Patient has type 2 diabetes mellitus and hypertension, "
    "currently controlled on metformin."
)


def test_all_evidence_quotes_valid():
    candidate = {
        "conditions": [
            {"evidence_quote": "type 2 diabetes mellitus"},
            {"evidence_quote": "hypertension"},
        ]
    }
    assert evidence_quotes_valid(candidate, _NOTE) is True


def test_one_invalid_evidence_quote():
    candidate = {
        "conditions": [
            {"evidence_quote": "type 2 diabetes mellitus"},
            {"evidence_quote": "asthma"},
        ]
    }
    assert evidence_quotes_valid(candidate, _NOTE) is False


def test_no_evidence_quotes_is_valid():
    assert evidence_quotes_valid({"conditions": []}, _NOTE) is True


def test_missing_evidence_quote_is_invalid():
    candidate = {
        "conditions": [
            {"condition_name": "Type 2 diabetes mellitus"},
        ]
    }
    assert evidence_quotes_valid(candidate, _NOTE) is False


def test_meaningful_change_medication():
    cached = _cached("Patient takes metformin 500 mg daily")
    assert has_meaningful_change("Patient takes insulin 10 units daily", cached) is True


def test_meaningful_change_dosage_numeric():
    cached = _cached("Patient takes metformin 500 mg daily")
    assert has_meaningful_change("Patient takes metformin 1000 mg daily", cached) is True


def test_meaningful_change_allergy():
    cached = _cached("Patient with no known allergies")
    assert has_meaningful_change("Patient with penicillin allergy", cached) is True


def test_meaningful_change_diagnosis():
    cached = _cached("Patient has hypertension")
    assert has_meaningful_change("Patient has hypertension and asthma", cached) is True


def test_meaningful_change_negation_added():
    cached = _cached("Patient has diabetes")
    assert has_meaningful_change("Patient does not have diabetes", cached) is True


def test_meaningful_change_negation_removed():
    cached = _cached("Patient does not have diabetes")
    assert has_meaningful_change("Patient has diabetes", cached) is True


def test_meaningful_change_numeric_value_higher():
    cached = _cached("Blood pressure 120 80")
    assert has_meaningful_change("Blood pressure 150 90", cached) is True


def test_meaningful_change_numeric_value_lower():
    cached = _cached("Blood pressure 150 90")
    assert has_meaningful_change("Blood pressure 120 80", cached) is True


def test_meaningful_change_medication_removed():
    cached = _cached("Patient takes metformin daily")
    assert has_meaningful_change("Patient takes no medication", cached) is True


def test_meaningful_change_medication_replaced_unknown_term():
    cached = _cached("Patient takes metformin daily")
    assert has_meaningful_change("Patient takes ozempic daily", cached) is True


def test_meaningful_change_diagnosis_removed():
    cached = _cached("Patient has asthma")
    assert has_meaningful_change("Patient no longer has asthma", cached) is True


def test_meaningful_change_diagnosis_replaced():
    cached = _cached("Patient has asthma")
    assert has_meaningful_change("Patient has copd", cached) is True


def test_meaningful_change_allergy_removed():
    cached = _cached("Patient has penicillin allergy")
    assert has_meaningful_change("Patient has no allergies", cached) is True


def test_meaningful_change_demographic_sex():
    cached = _cached("Patient is a 64 year old male with type 2 diabetes")
    assert has_meaningful_change(
        "Patient is a 64 year old female with type 2 diabetes", cached
    ) is True


def test_meaningful_change_demographic_pregnancy():
    cached = _cached("Patient with type 2 diabetes")
    assert has_meaningful_change(
        "Patient is pregnant with type 2 diabetes", cached
    ) is True


def test_no_meaningful_change_benign_rewording():
    cached = _cached(
        "Patient has type 2 diabetes and hypertension controlled on metformin"
    )
    assert has_meaningful_change(
        "Patient has type 2 diabetes and hypertension managed on metformin", cached
    ) is False


def test_meaningful_change_missing_shingles_is_true():
    assert has_meaningful_change("Patient takes insulin", None) is True
    assert has_meaningful_change("Patient takes insulin", []) is True


def test_default_similarity_threshold_is_0_95():
    assert DEFAULT_SIMILARITY_THRESHOLD == 0.95


def test_select_valid_candidate_above_threshold():
    cand = _candidate(BASE, summary="base")
    new = BASE.replace("reports good adherence", "reports excellent adherence")
    assert select_best_similar_candidate(new, [cand]) is cand


def test_select_candidate_at_exact_threshold_is_accepted():
    base_text = (
        "patient is a 64 year old male with type 2 diabetes mellitus hypertension "
        "and hyperlipidemia managed on metformin lisinopril atorvastatin with "
        "adequate glycemic control and stable vitals no known drug allergies "
        "followed in clinic for routine care reports good adherence to"
    )
    new_text = base_text.replace("patient ", "client ", 1)
    cand = _candidate(base_text, summary="base")
    assert select_best_similar_candidate(new_text, [cand]) is cand


def test_select_candidate_below_threshold():
    other = "pediatric patient with acute otitis media treated with amoxicillin"
    cand = _candidate(other, summary="other")
    assert select_best_similar_candidate(BASE, [cand]) is None


def test_select_high_similarity_invalid_evidence():
    cand = _candidate(BASE, summary="base")
    new = BASE.replace(" mellitus ", " mellitis ")
    assert select_best_similar_candidate(new, [cand]) is None


def test_select_high_similarity_meaningful_medical_change():
    cand = _candidate(BASE, summary="base")
    new = BASE.replace("lisinopril", "insulin")
    assert select_best_similar_candidate(new, [cand]) is None


def test_select_best_safe_candidate():
    new = BASE.replace("reports good adherence", "reports excellent adherence")
    cand_a = _candidate(BASE, summary="a")
    cand_b = _candidate(new, summary="b")
    assert select_best_similar_candidate(new, [cand_a, cand_b]) is cand_b


def test_select_no_valid_candidate():
    assert select_best_similar_candidate(BASE, []) is None
    cand = {"conditions": [{"evidence_quote": QUOTE}], "summary": "x"}
    assert select_best_similar_candidate(BASE, [cand]) is None
