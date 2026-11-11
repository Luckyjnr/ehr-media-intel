"""Turn cleaned Task 1 records into FHIR resources (plain JSON dicts).

The mapper only builds dicts. validate.py then builds each dict through
fhir.resources, so mapping and checking stay separate and easy to test.
Fields with no value are left out, because FHIR does not allow empty values.
"""

import base64
import uuid

from app.ingestion.models import ClinicalDocument, LabResult, PatientRecord

# Placeholder system for our MRNs. A real hospital uses its own URI or OID here.
# example.org is reserved for examples, so it can never clash with a real system.
MRN_SYSTEM = "http://example.org/fhir/mrn"
LOINC_SYSTEM = "http://loinc.org"

# Fixed namespace for uuid5. Same input -> same UUID on every run, so
# re-running the build gives identical bundles (easy to compare and debug).
UUID_NAMESPACE = uuid.UUID("6f1c3a52-5d0e-4b7a-9a43-2f6f3c1e8d10")

DOC_TYPE_TEXT = {
    "discharge_summary": "Discharge summary",
    "imaging": "Imaging report",
    "lab_report": "Lab report",
    "clinical_note": "Clinical note",
    "other": "Other document",
}


def make_uuid(resource_type: str, key: str) -> str:
    return str(uuid.uuid5(UUID_NAMESPACE, f"{resource_type}:{key}"))


def reference_to(resource_uuid: str) -> dict:
    """A reference to another resource in the same bundle.

    Resources in a bundle have no server URL yet, so they point at each
    other with "urn:uuid:..." which matches the entry's fullUrl.
    """
    return {"reference": f"urn:uuid:{resource_uuid}"}


def map_patient(patient: PatientRecord, patient_uuid: str) -> dict:
    resource = {
        "resourceType": "Patient",
        "id": patient_uuid,
        "identifier": [
            {
                # "MR" = Medical Record Number in the standard HL7 identifier types.
                "type": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                            "code": "MR",
                        }
                    ]
                },
                "system": MRN_SYSTEM,
                "value": patient.mrn,
            }
        ],
        "gender": patient.gender,  # Task 1 already uses the FHIR gender codes
    }

    name = {}
    if patient.family_name:
        name["family"] = patient.family_name
    if patient.given_name:
        # FHIR allows several given names, so it is a list.
        name["given"] = [patient.given_name]
    if name:
        resource["name"] = [name]

    if patient.birth_date:
        resource["birthDate"] = patient.birth_date.isoformat()

    return resource


def map_encounter(encounter_id: str, encounter_uuid: str, patient_uuid: str) -> dict:
    return {
        "resourceType": "Encounter",
        "id": encounter_uuid,
        "identifier": [{"value": encounter_id}],
        # Our documents come from completed visits.
        "status": "finished",
        # "class" (inpatient, outpatient...) is required, but Task 1 did not keep it.
        # We say "unknown" honestly instead of guessing.
        "class": {
            "system": "http://terminology.hl7.org/CodeSystem/v3-NullFlavor",
            "code": "UNK",
            "display": "unknown",
        },
        "subject": reference_to(patient_uuid),
    }


def map_document(
    doc: ClinicalDocument,
    doc_uuid: str,
    patient_uuid: str,
    encounter_uuid: str | None,
) -> dict:
    # FHIR attachments carry their data as base64 text.
    encoded_text = base64.b64encode(doc.text.encode("utf-8")).decode("ascii")

    attachment = {"contentType": "text/plain", "data": encoded_text}
    if doc.title:
        attachment["title"] = doc.title

    resource = {
        "resourceType": "DocumentReference",
        "id": doc_uuid,
        "status": "current",
        "type": {"text": DOC_TYPE_TEXT[doc.doc_type]},
        "subject": reference_to(patient_uuid),
        "content": [{"attachment": attachment}],
    }
    if doc.title:
        resource["description"] = doc.title

    # DocumentReference.date must include a time and time zone, and we only
    # know the day. context.period.start accepts a plain date, so it goes there.
    context = {}
    if encounter_uuid:
        context["encounter"] = [reference_to(encounter_uuid)]
    if doc.date:
        context["period"] = {"start": doc.date.isoformat()}
    if context:
        resource["context"] = context

    return resource


def lab_conclusion(lab: LabResult) -> str:
    """One short readable line, e.g. 'Hemoglobin: 11.20 g/dL (low). Reference range: 12.0-15.5.'"""
    name = lab.test_name or "Unknown test"
    value_part = lab.value or "no value"
    if lab.unit:
        value_part = f"{value_part} {lab.unit}"

    text = f"{name}: {value_part}"
    if lab.flag:
        text = f"{text} ({lab.flag})"
    text = f"{text}."
    if lab.reference_range:
        text = f"{text} Reference range: {lab.reference_range}."
    return text


def map_lab(lab: LabResult, lab_uuid: str, patient_uuid: str) -> dict:
    # The brief asks for one DiagnosticReport per lab with the result in the
    # conclusion. (A fuller model would add an Observation per value.)
    code = {"text": lab.test_name or "Unknown test"}
    if lab.loinc_code:
        coding = {"system": LOINC_SYSTEM, "code": lab.loinc_code}
        if lab.test_name:
            coding["display"] = lab.test_name
        code["coding"] = [coding]

    resource = {
        "resourceType": "DiagnosticReport",
        "id": lab_uuid,
        "status": "final",
        "code": code,
        "subject": reference_to(patient_uuid),
        "conclusion": lab_conclusion(lab),
    }
    if lab.collected_at:
        resource["effectiveDateTime"] = lab.collected_at.isoformat()
    return resource
