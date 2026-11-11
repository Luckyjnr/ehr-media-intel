"""Task 2 tests. Small records are built here; no data files are read."""

import datetime as dt

from app.fhir.build import run_build
from app.fhir.bundle import build_patient_bundle
from app.fhir.mapper import MRN_SYSTEM, make_uuid, map_patient
from app.fhir.store import get_bundle, list_bundles, save_bundle
from app.fhir.validate import check_references, validate_bundle
from app.ingestion.models import ClinicalDocument, Conflict, LabResult, PatientRecord


def make_patient(mrn="0012345", given="John", family="Smith"):
    return PatientRecord(
        id=f"p-{mrn}-{given}",
        mrn=mrn,
        given_name=given,
        family_name=family,
        birth_date=dt.date(1958, 4, 12),
        gender="male",
    )


def make_document(mrn="0012345", encounter_id="ENC-1"):
    return ClinicalDocument(
        id=f"doc-{mrn}",
        mrn=mrn,
        doc_type="discharge_summary",
        date=dt.date(2024, 3, 5),
        title="Discharge Summary",
        text="Patient stable.",
        encounter_id=encounter_id,
        source_file="ehr_export.json",
    )


def make_lab(mrn="0012345"):
    return LabResult(
        id=f"lab-{mrn}",
        mrn=mrn,
        test_name="Hemoglobin A1c",
        loinc_code="4548-4",
        value="7.2",
        unit="%",
        reference_range="4.0-5.6",
        flag="high",
        collected_at=dt.date(2024, 3, 5),
        source_file="lab_results.csv",
    )


def test_patient_maps_mrn_gender_and_birth_date():
    resource = map_patient(make_patient(), make_uuid("Patient", "0012345"))

    identifier = resource["identifier"][0]
    assert identifier["system"] == MRN_SYSTEM
    assert identifier["value"] == "0012345"
    assert identifier["type"]["coding"][0]["code"] == "MR"
    assert resource["gender"] == "male"
    assert resource["birthDate"] == "1958-04-12"
    assert resource["name"] == [{"family": "Smith", "given": ["John"]}]


def test_every_reference_in_a_bundle_resolves():
    bundle = build_patient_bundle(make_patient(), [make_document()], [make_lab()])

    types = [entry["resource"]["resourceType"] for entry in bundle["entry"]]
    assert types == ["Patient", "Encounter", "DocumentReference", "DiagnosticReport"]
    assert check_references(bundle) == []
    assert validate_bundle(bundle) == []

    # The document points at both the patient and the encounter.
    patient_url = bundle["entry"][0]["fullUrl"]
    encounter_url = bundle["entry"][1]["fullUrl"]
    document = bundle["entry"][2]["resource"]
    assert document["subject"]["reference"] == patient_url
    assert document["context"]["encounter"][0]["reference"] == encounter_url

    # A reference to something outside the bundle is caught.
    document["subject"]["reference"] = "urn:uuid:not-in-this-bundle"
    errors = check_references(bundle)
    assert len(errors) == 1
    assert "does not point to anything" in errors[0]


def test_bad_resource_is_reported_not_crashed_on():
    bundle = build_patient_bundle(make_patient(), [], [])
    bundle["entry"][0]["resource"]["birthDate"] = "1958-13-45"  # not a real date
    bundle["entry"][0]["resource"]["favouriteColour"] = "blue"  # not a FHIR field

    errors = validate_bundle(bundle)  # must not raise

    assert len(errors) == 2
    assert any("birthDate" in error for error in errors)
    assert any("favouriteColour" in error for error in errors)


def test_mrn_conflict_patient_gets_no_bundle_and_is_reported():
    david = make_patient(mrn="0090123", given="David", family="Lee")
    daniel = make_patient(mrn="0090123", given="Daniel", family="Lee")
    clean = make_patient()
    conflict = Conflict(
        conflict_type="MRN_CONFLICT",
        shared_value="0090123",
        differing_fields=["given_name"],
        patient_ids=[david.id, daniel.id],
        records=[],
        reason="MRN 0090123 is used by 2 records.",
    )

    passed, report = run_build(
        [david, daniel, clean], [make_document(mrn="0090123")], [], [conflict]
    )

    assert [mrn for mrn, _ in passed] == ["0012345"]
    assert report["summary"]["held_for_review"] == 1
    assert report["held_for_review"][0]["mrn"] == "0090123"
    assert report["held_for_review"][0]["patient_ids"] == [david.id, daniel.id]
    # The held patient's document is not reported as "unlinked".
    assert report["unlinked_records"] == []


def test_store_save_then_get_returns_the_same_bundle(tmp_path):
    db_path = tmp_path / "fhir.db"
    bundle = build_patient_bundle(make_patient(), [make_document()], [make_lab()])

    save_bundle("0012345", bundle, db_path)
    save_bundle("0012345", bundle, db_path)  # saving again replaces, not duplicates

    assert get_bundle("0012345", db_path) == bundle
    assert get_bundle("9999999", db_path) is None
    listing = list_bundles(db_path)
    assert len(listing) == 1
    assert listing[0]["mrn"] == "0012345"
    assert listing[0]["resource_count"] == 4
