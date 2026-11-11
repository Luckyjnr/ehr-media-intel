"""Put one patient's resources into one FHIR Bundle of type "collection".

A "collection" bundle is just a group of resources to keep together.
It is not a transaction, so no server is needed to read it.
"""

from app.fhir.mapper import (
    make_uuid,
    map_document,
    map_encounter,
    map_lab,
    map_patient,
)
from app.ingestion.models import ClinicalDocument, LabResult, PatientRecord


def _entry(resource: dict) -> dict:
    # fullUrl must match what other resources use in their references.
    return {"fullUrl": f"urn:uuid:{resource['id']}", "resource": resource}


def build_patient_bundle(
    patient: PatientRecord,
    documents: list[ClinicalDocument],
    labs: list[LabResult],
) -> dict:
    """Build the bundle for one patient. documents and labs must be that patient's."""
    patient_uuid = make_uuid("Patient", patient.mrn)
    entries = [_entry(map_patient(patient, patient_uuid))]

    # One Encounter per distinct encounter_id found in the documents.
    encounter_uuids: dict[str, str] = {}
    for doc in documents:
        if doc.encounter_id and doc.encounter_id not in encounter_uuids:
            encounter_uuid = make_uuid("Encounter", f"{patient.mrn}:{doc.encounter_id}")
            encounter_uuids[doc.encounter_id] = encounter_uuid
            entries.append(_entry(map_encounter(doc.encounter_id, encounter_uuid, patient_uuid)))

    for doc in documents:
        encounter_uuid = None
        if doc.encounter_id:
            encounter_uuid = encounter_uuids[doc.encounter_id]
        doc_uuid = make_uuid("DocumentReference", doc.id)
        entries.append(_entry(map_document(doc, doc_uuid, patient_uuid, encounter_uuid)))

    for lab in labs:
        lab_uuid = make_uuid("DiagnosticReport", lab.id)
        entries.append(_entry(map_lab(lab, lab_uuid, patient_uuid)))

    return {
        "resourceType": "Bundle",
        "id": make_uuid("Bundle", patient.mrn),
        "type": "collection",
        "entry": entries,
    }
