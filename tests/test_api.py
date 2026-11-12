"""Task 4 tests: search API and patient lookup.

Fast and offline: a fake embedder replaces the real model, and the database
and vector store live in a temporary folder.
"""

import datetime as dt
import hashlib
import re

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.fhir.bundle import build_patient_bundle
from app.fhir.store import save_bundle
from app.ingestion.models import ClinicalDocument, LabResult, PatientRecord
from app.search.index import build_index
from app.summaries.cache import record_hash, save_summary
from app.summaries.summarizer import DISCLAIMER, MOCK_MODEL, summarize_mock

VECTOR_SIZE = 64


def fake_embed(texts: list[str]) -> list[list[float]]:
    """Bag-of-words vectors: each word adds 1 to a fixed slot.

    Texts that share words get similar vectors, which is enough to test
    ranking and filters without downloading the real model.
    """
    vectors = []
    for text in texts:
        vector = [0.0] * VECTOR_SIZE
        for word in re.findall(r"[a-z]+", text.lower()):
            # md5, not hash(): Python's hash() changes between runs.
            slot = int(hashlib.md5(word.encode()).hexdigest(), 16) % VECTOR_SIZE
            vector[slot] += 1.0
        if not any(vector):
            vector[0] = 1.0  # an all-zero vector has no direction
        vectors.append(vector)
    return vectors


# (mrn, first, last, date, document text, lab name, lab value, flag)
PATIENTS = [
    ("0000001", "Ann", "Lee", dt.date(2024, 1, 10), "High blood glucose, diabetes follow up.", "Glucose", "250", "high"),
    ("0000002", "Bob", "Ray", dt.date(2024, 3, 5), "MRI brain for memory loss.", "TSH", "2.0", "normal"),
    ("0000003", "Cat", "Fox", dt.date(2024, 6, 20), "Chest X-ray for cough.", "Hemoglobin", "10.1", "low"),
]


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "fhir.db"
    chroma_path = tmp_path / "chroma"

    for mrn, first, last, date, text, lab_name, value, flag in PATIENTS:
        patient = PatientRecord(id=f"p-{mrn}", mrn=mrn, given_name=first, family_name=last, gender="unknown")
        document = ClinicalDocument(
            id=f"d-{mrn}", mrn=mrn, doc_type="clinical_note", date=date, title="Note",
            text=text, source_file="test",
        )
        lab = LabResult(
            id=f"l-{mrn}", mrn=mrn, test_name=lab_name, value=value, unit="x",
            flag=flag, collected_at=date, source_file="test",
        )
        bundle = build_patient_bundle(patient, [document], [lab])
        save_bundle(mrn, bundle, db_path)
        save_summary(summarize_mock(bundle).summary, record_hash(bundle, MOCK_MODEL), db_path)

    # 3 patients x (document + lab + summary) = 9 search items
    build_index(fake_embed, db_path, chroma_path)

    app = create_app(embedder=fake_embed, chroma_path=chroma_path, db_path=db_path)
    with TestClient(app) as test_client:  # "with" runs the startup code
        yield test_client


def test_search_returns_at_most_5_results_sorted_by_score(client):
    response = client.post("/search", json={"query": "blood glucose diabetes"})

    assert response.status_code == 200
    body = response.json()
    results = body["results"]
    assert len(results) == 5  # 9 items indexed, top 5 returned
    scores = [result["score"] for result in results]
    assert scores == sorted(scores, reverse=True)
    assert results[0]["mrn"] == "0000001"  # the diabetes note is the closest match
    assert results[0]["summary_snippet"]  # each card gets the patient's summary snippet
    assert body["took_ms"] >= 0


def test_resource_type_filter_only_returns_that_type(client):
    response = client.post("/search?resource_type=DiagnosticReport", json={"query": "lab result"})

    results = response.json()["results"]
    assert len(results) == 3
    assert {result["resource_type"] for result in results} == {"DiagnosticReport"}


def test_date_range_filter_only_returns_items_inside_the_range(client):
    response = client.post(
        "/search?date_from=2024-03-01&date_to=2024-03-31", json={"query": "patient"}
    )

    results = response.json()["results"]
    assert len(results) == 3  # Bob's document, lab and summary
    for result in results:
        assert "2024-03-01" <= result["date"] <= "2024-03-31"


def test_empty_query_and_bad_dates_return_422(client):
    blank = client.post("/search", json={"query": "   "})
    assert blank.status_code == 422
    assert "query must not be empty" in blank.text

    bad_date = client.post("/search?date_from=2024-13-45", json={"query": "glucose"})
    assert bad_date.status_code == 422
    assert "date_from" in bad_date.text

    backwards = client.post("/search?date_from=2024-06-01&date_to=2024-01-01", json={"query": "glucose"})
    assert backwards.status_code == 422
    assert "date_from must be on or before date_to" in backwards.text


def test_get_patient_returns_summary_with_disclaimer(client):
    response = client.get("/patients/0000002")

    assert response.status_code == 200
    body = response.json()
    assert body["patient_name"] == "Bob Ray"
    assert body["summary"]["disclaimer"] == DISCLAIMER
    types = [resource["type"] for resource in body["resources"]]
    assert types == ["Patient", "DocumentReference", "DiagnosticReport"]


def test_get_unknown_patient_returns_404(client):
    response = client.get("/patients/9999999")

    assert response.status_code == 404
    assert "9999999" in response.json()["detail"]
