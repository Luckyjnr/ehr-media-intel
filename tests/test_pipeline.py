"""Tests for record cleaning, duplicate removal and identity conflicts.

Inputs are small raw dicts built here, in the same shape the loaders produce.
"""

import datetime as dt

from app.ingestion.pipeline import run_pipeline

TODAY = dt.date(2026, 1, 1)


def ehr_patient(source_id, mrn, first, last, dob, sex="M"):
    return {
        "source_id": source_id,
        "source_file": "ehr_export.json",
        "mrn": mrn,
        "given_name": first,
        "family_name": last,
        "birth_date": dob,
        "gender": sex,
    }


def csv_patient(source_id, mrn, full_name, dob):
    # The CSV has no gender column, so there is no "gender" key.
    return {
        "source_id": source_id,
        "source_file": "lab_results.csv",
        "mrn": mrn,
        "full_name": full_name,
        "birth_date": dob,
    }


def document(source_id, mrn, text="Patient stable.", source_file="ehr_export.json"):
    return {
        "source_id": source_id,
        "source_file": source_file,
        "mrn": mrn,
        "doc_type": "Discharge Summary",
        "date": "2024-03-05",
        "title": "Discharge Summary",
        "text": text,
        "encounter_id": "ENC-1",
    }


def lab(source_id, mrn, value, collected, source_file):
    return {
        "source_id": source_id,
        "source_file": source_file,
        "mrn": mrn,
        "test_name": "Hemoglobin",
        "loinc_code": "718-7",
        "value": value,
        "unit": "g/dL",
        "reference_range": "12.0-15.5",
        "flag": "L",
        "collected_at": collected,
    }


def test_missing_mrn_goes_to_rejected_not_cleaned():
    result = run_pipeline(
        raw_patients=[ehr_patient("p1", None, "Kevin", "Taylor", "1960-01-22")],
        raw_documents=[document("d1", "")],
        raw_labs=[lab("l1", "   ", "13", "2024-03-05", "lab_results.csv")],
        today=TODAY,
    )
    assert result.patients == []
    assert result.documents == []
    assert result.labs == []
    assert len(result.rejected) == 3
    assert {r.record_type for r in result.rejected} == {"patient", "document", "lab"}
    assert result.rejected[0].audit[0].rule == "MRN_MISSING"


def test_exact_duplicate_documents_are_removed_and_logged():
    result = run_pipeline(
        raw_patients=[],
        raw_documents=[document("d1", "12345"), document("d2", "MRN-0012345"), document("d3", "12345", text="Different.")],
        raw_labs=[],
        today=TODAY,
    )
    assert len(result.documents) == 2
    assert result.duplicates_dropped["documents"] == 1
    kept = result.documents[0]
    assert kept.id == "d1"
    assert "DUPLICATE_DROPPED" in [e.rule for e in kept.audit]


def test_exact_duplicate_patients_are_removed():
    same = ehr_patient("ehr_export.json:patient[0]", "12345", "John", "Smith", "1958-04-12")
    copy = dict(same, source_id="ehr_export.json:patient[1]")
    result = run_pipeline([same, copy], [], [], today=TODAY)
    assert len(result.patients) == 1
    assert result.duplicates_dropped["patients"] == 1
    assert result.patient_mentions_linked == 0
    assert result.patients[0].source_ids == ["ehr_export.json:patient[0]", "ehr_export.json:patient[1]"]


def test_same_lab_in_json_and_csv_counts_as_duplicate():
    result = run_pipeline(
        raw_patients=[],
        raw_documents=[],
        raw_labs=[
            lab("json-lab", "MRN-0023456", "11.20", "2024-03-05", "ehr_export.json"),
            lab("csv-lab", " mrn 23456 ", "11.2", "05-Mar-2024", "lab_results.csv"),
        ],
        today=TODAY,
    )
    assert len(result.labs) == 1
    assert result.labs[0].id == "json-lab"
    assert result.duplicates_dropped["labs"] == 1
    assert "CROSS_SOURCE_DUPLICATE" in [e.rule for e in result.labs[0].audit]


def test_same_mrn_with_different_dob_is_flagged_and_not_merged():
    result = run_pipeline(
        raw_patients=[
            ehr_patient("p1", "90123", "David", "Lee", "1977-09-09"),
            csv_patient("p2", "0090123", "Lee, David", "09/19/1977"),
        ],
        raw_documents=[],
        raw_labs=[],
        today=TODAY,
    )
    # Both records are kept.
    assert len(result.patients) == 2
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.conflict_type == "MRN_CONFLICT"
    assert conflict.shared_value == "0090123"
    assert conflict.differing_fields == ["birth_date"]
    # Each record carries its own warning.
    for patient in result.patients:
        assert "MRN_CONFLICT" in [e.rule for e in patient.audit]


def test_same_name_and_dob_with_different_mrns_is_a_possible_duplicate():
    result = run_pipeline(
        raw_patients=[
            ehr_patient("p1", "0101234", "Susan", "Clark", "1988-06-15", sex="F"),
            csv_patient("p2", "101235", "CLARK, SUSAN", "06/15/1988"),
        ],
        raw_documents=[],
        raw_labs=[],
        today=TODAY,
    )
    assert len(result.patients) == 2
    assert [c.conflict_type for c in result.conflicts] == ["POSSIBLE_DUPLICATE_PATIENT"]


def test_csv_mention_of_a_known_patient_is_linked_not_duplicated():
    result = run_pipeline(
        raw_patients=[
            ehr_patient("p1", "12345", "  john ", "SMITH", "1958-04-12"),
            csv_patient("p2", "MRN-0012345", "SMITH, JOHN", "04/12/1958"),
        ],
        raw_documents=[],
        raw_labs=[],
        today=TODAY,
    )
    assert len(result.patients) == 1
    patient = result.patients[0]
    assert patient.gender == "male"  # from the EHR, the first source
    assert patient.source_ids == ["p1", "p2"]
    assert result.patient_mentions_linked == 1
    assert result.conflicts == []


def test_each_record_carries_its_own_audit_log():
    result = run_pipeline(
        raw_patients=[ehr_patient("p1", "12345", "john", "smith", "July 4 1945", sex="1")],
        raw_documents=[],
        raw_labs=[],
        today=TODAY,
    )
    patient = result.patients[0]
    assert patient.birth_date == dt.date(1945, 7, 4)
    audit_rules = {e.rule for e in patient.audit}
    assert {"MRN_NORMALIZED", "NAME_NORMALIZED", "DATE_NORMALIZED", "GENDER_NORMALIZED"} <= audit_rules
    assert all(e.record_id == "p1" for e in patient.audit)
