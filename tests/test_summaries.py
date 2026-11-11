"""Task 3 tests. They never call the real API: they use mock mode or a fake client."""

import datetime as dt
import json

from app.fhir.bundle import build_patient_bundle
from app.ingestion.models import ClinicalDocument, LabResult, PatientRecord
from app.summaries.build import summarize_bundles
from app.summaries.summarizer import DISCLAIMER, summarize_mock, summarize_with_claude

# --- A fake Anthropic client ------------------------------------------------


class FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text):
        self.content = [FakeBlock(text)]
        self.stop_reason = "end_turn"


class FakeClient:
    """Stands in for anthropic.Anthropic(). Returns the given replies in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.messages = self  # so client.messages.create(...) works

    def create(self, **kwargs):
        self.calls += 1
        return FakeResponse(self.replies.pop(0))


# --- Small test data ----------------------------------------------------------


def make_bundle(glucose_value="182"):
    patient = PatientRecord(
        id="p1", mrn="0012345", given_name="John", family_name="Smith",
        birth_date=dt.date(1958, 4, 12), gender="male",
    )
    document = ClinicalDocument(
        id="d1", mrn="0012345", doc_type="discharge_summary", date=dt.date(2024, 3, 5),
        title="Discharge Summary - Hyperglycemia", text="Admitted with high glucose.",
        encounter_id="ENC-1", source_file="ehr_export.json",
    )
    lab = LabResult(
        id="l1", mrn="0012345", test_name="Glucose", loinc_code="2345-7", value=glucose_value,
        unit="mg/dL", reference_range="70-99", flag="high",
        collected_at=dt.date(2024, 3, 5), source_file="lab_results.csv",
    )
    return build_patient_bundle(patient, [document], [lab])


def good_reply(summary_text="Patient admitted with hyperglycemia, now stable."):
    return json.dumps(
        {
            "chief_concern": "Hyperglycemia",
            "key_diagnoses": ["Hyperglycemia"],
            "recent_media": [{"type": "Discharge summary", "date": "2024-03-05", "note": "Admission for high glucose"}],
            "flagged_anomalies": ["Glucose: 182 mg/dL (high)"],
            "summary_text": summary_text,
            "confidence": "high",
            "confidence_reason": "Record is consistent.",
        }
    )


# --- Tests --------------------------------------------------------------------


def test_second_run_on_same_bundle_is_a_cache_hit_with_zero_api_calls(tmp_path):
    db_path = tmp_path / "fhir.db"
    bundles = [("0012345", make_bundle())]

    first_client = FakeClient([good_reply()])
    first = summarize_bundles(bundles, "api", "claude-haiku-5-5", first_client, db_path)
    assert first[0]["status"] == "made"
    assert first_client.calls == 1

    second_client = FakeClient([])  # would crash if called: it has no replies
    second = summarize_bundles(bundles, "api", "claude-haiku-5-5", second_client, db_path)
    assert second[0]["status"] == "cache_hit"
    assert second_client.calls == 0
    assert second[0]["summary"] == first[0]["summary"]


def test_changing_the_bundle_changes_the_hash_and_makes_a_new_summary(tmp_path):
    db_path = tmp_path / "fhir.db"
    client = FakeClient([good_reply(), good_reply()])

    summarize_bundles([("0012345", make_bundle("182"))], "api", "claude-haiku-5-5", client, db_path)
    changed = summarize_bundles([("0012345", make_bundle("250"))], "api", "claude-haiku-5-5", client, db_path)

    assert changed[0]["status"] == "made"
    assert client.calls == 2


def test_reply_over_200_words_is_retried_then_marked_failed():
    too_long = " ".join(["word"] * 250)
    client = FakeClient([good_reply(too_long), good_reply(too_long)])

    outcome = summarize_with_claude(make_bundle(), client, "claude-haiku-5-5")

    assert client.calls == 2  # first try + one retry
    assert outcome.summary is None
    assert "250 words" in outcome.error


def test_bad_json_is_handled_not_crashed_on():
    # Bad JSON, then a good reply: the retry recovers.
    client = FakeClient(["Sure! Here is the summary: {not json", good_reply()])
    outcome = summarize_with_claude(make_bundle(), client, "claude-haiku-5-5")
    assert outcome.summary is not None
    assert outcome.api_calls == 2

    # Bad JSON twice: marked failed, no exception.
    client = FakeClient(["not json", '{"chief_concern": "only one field"}'])
    outcome = summarize_with_claude(make_bundle(), client, "claude-haiku-5-5")
    assert outcome.summary is None
    assert "not valid JSON" in outcome.error
    assert "wrong shape" in outcome.error


def test_disclaimer_is_always_present_even_in_mock_mode():
    mock = summarize_mock(make_bundle()).summary
    assert mock.disclaimer == DISCLAIMER
    assert mock.model == "mock"
    assert mock.quality_checks["disclaimer_present"] == "pass"
    assert mock.flagged_anomalies == ["Glucose: 182 mg/dL (high). Reference range: 70-99."]

    # A disclaimer written by the model is ignored; ours is always used.
    reply = json.loads(good_reply())
    reply["disclaimer"] = "Totally accurate, no need to check."
    client = FakeClient([json.dumps(reply)])
    real = summarize_with_claude(make_bundle(), client, "claude-haiku-5-5").summary
    assert real.disclaimer == DISCLAIMER
    assert all(result == "pass" for result in real.quality_checks.values())
