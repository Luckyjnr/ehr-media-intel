"""Duplicate removal and patient identity conflict detection.

Two very different jobs live here:

1. Duplicates of the SAME facts (an export that repeats a row, or the same lab
   in both the JSON and the CSV). These are safe to drop: no information is lost.
   We keep the first copy and log where the dropped copy came from.

2. Identity conflicts, where records disagree about WHO a patient is
   (same MRN with a different DOB or name, or the same person under two MRNs).
   We do NOT auto-merge these. In healthcare, merging two different people
   puts one person's allergies, meds and results in the other person's chart,
   which can directly harm a patient. Un-merging later is hard and risky.
   Leaving two records apart is the safe failure: a human reviews conflicts.json
   and decides. So we keep both records and flag them.
"""

import hashlib
import json
from typing import Any

from app.ingestion.audit import make_entry
from app.ingestion.models import ClinicalDocument, Conflict, LabResult, PatientRecord


def content_hash(values: dict[str, Any]) -> str:
    """A fingerprint of a record's cleaned content.

    sort_keys makes the hash independent of key order. default=str turns
    dates into text so json can handle them.
    """
    text = json.dumps(values, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Patients
# ---------------------------------------------------------------------------


def _patient_content(patient: PatientRecord) -> dict[str, Any]:
    # Only the facts, not where they came from (id, source_ids, audit).
    return {
        "mrn": patient.mrn,
        "given_name": patient.given_name,
        "family_name": patient.family_name,
        "birth_date": patient.birth_date,
        "gender": patient.gender,
    }


def source_file_of(record_id: str) -> str:
    """Get the file name back out of a record id.

    Ids look like "ehr_export.json:patient[4]" or "notes/note_x.txt#patient",
    so the file is everything before the first ":" or "#".
    """
    file_part = record_id.split("#")[0]
    file_part = file_part.split(":")[0]
    return file_part


def _identity_key(patient: PatientRecord) -> tuple:
    # Gender is not part of identity: the CSV and the notes do not carry it.
    return (patient.mrn, patient.given_name, patient.family_name, patient.birth_date)


def _absorb(survivor: PatientRecord, other: PatientRecord, rule: str, reason: str, severity: str) -> None:
    """Record that `other` describes the same patient as `survivor`.

    We keep other's source ids and audit entries, so no history is lost.
    """
    survivor.source_ids.extend(other.source_ids)
    survivor.audit.extend(other.audit)
    survivor.audit.append(
        make_entry(other.id, "record", other.id, survivor.id, rule, reason, severity)
    )


def build_patient_index(mentions: list[PatientRecord]) -> tuple[list[PatientRecord], int, int]:
    """Combine patient mentions that agree on MRN + name + DOB.

    Returns (patients, exact_duplicates_dropped, mentions_linked).

    The input order matters: the first mention becomes the kept record, so
    the pipeline passes EHR records first (the EHR is the source of truth
    for demographics).
    """
    # Mentions with a DOB first, so a mention without a DOB can be linked
    # to a full record that has one.
    with_dob = [m for m in mentions if m.birth_date is not None]
    without_dob = [m for m in mentions if m.birth_date is None]
    ordered = with_dob + without_dob

    patients: list[PatientRecord] = []
    by_identity: dict[tuple, PatientRecord] = {}
    exact_duplicates = 0
    linked = 0

    for mention in ordered:
        survivor = by_identity.get(_identity_key(mention))

        # Case 1: identical content AND from the same file -> the source
        # repeated a row. (Identical content from a different file is the
        # same patient seen in another source, which is case 2.)
        if survivor is not None:
            same_content = content_hash(_patient_content(mention)) == content_hash(_patient_content(survivor))
            same_file = source_file_of(mention.id) == source_file_of(survivor.id)
            if same_content and same_file:
                _absorb(
                    survivor, mention, "DUPLICATE_DROPPED",
                    "Exact duplicate of an existing patient record in the same file. Dropped.", "info",
                )
                exact_duplicates += 1
                continue

        # Case 2: same MRN, name and DOB -> the same patient seen in another source.
        if survivor is not None:
            if mention.gender != "unknown" and survivor.gender != "unknown" and mention.gender != survivor.gender:
                survivor.audit.append(
                    make_entry(
                        mention.id, "gender", mention.gender, survivor.gender, "GENDER_MISMATCH",
                        "Sources disagree on gender. Kept the value from the first source.", "warning",
                    )
                )
            _absorb(
                survivor, mention, "SOURCE_LINKED",
                "Same MRN, name and DOB as an existing patient. Linked as another source.", "info",
            )
            linked += 1
            continue

        # Case 3: no DOB, but same MRN and same name. Missing is not the same
        # as different, so we link it, but as a warning so a human can check.
        if mention.birth_date is None:
            match = None
            for patient in patients:
                same_mrn = patient.mrn == mention.mrn
                same_name = patient.given_name == mention.given_name and patient.family_name == mention.family_name
                if same_mrn and same_name:
                    match = patient
                    break
            if match is not None:
                _absorb(
                    match, mention, "LINKED_WITHOUT_DOB",
                    "Source has no DOB, but MRN and name match an existing patient. Linked.", "warning",
                )
                linked += 1
                continue

        # Otherwise: a new patient record.
        patients.append(mention)
        by_identity[_identity_key(mention)] = mention

    return patients, exact_duplicates, linked


# ---------------------------------------------------------------------------
# Documents and labs
# ---------------------------------------------------------------------------


def _document_content(doc: ClinicalDocument) -> dict[str, Any]:
    return {
        "mrn": doc.mrn,
        "doc_type": doc.doc_type,
        "date": doc.date,
        "title": doc.title,
        "text": doc.text,
        "encounter_id": doc.encounter_id,
    }


def drop_duplicate_documents(documents: list[ClinicalDocument]) -> tuple[list[ClinicalDocument], int]:
    """Drop documents whose cleaned content is identical. Returns (kept, dropped_count)."""
    kept: list[ClinicalDocument] = []
    seen: dict[str, ClinicalDocument] = {}
    dropped = 0

    for doc in documents:
        doc_hash = content_hash(_document_content(doc))
        if doc_hash in seen:
            survivor = seen[doc_hash]
            survivor.audit.extend(doc.audit)
            survivor.audit.append(
                make_entry(
                    doc.id, "record", doc.id, survivor.id, "DUPLICATE_DROPPED",
                    "Exact duplicate of another document. Dropped.", "info",
                )
            )
            dropped += 1
        else:
            seen[doc_hash] = doc
            kept.append(doc)

    return kept, dropped


def _comparable_value(value: str | None) -> Any:
    # "11.20" and "11.2" are the same result, so compare numbers as numbers.
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return value.strip().lower()


def lab_match_key(lab: LabResult) -> tuple:
    """Two labs are the same result if MRN, test, collected date and value match.

    This catches the same lab arriving from both the JSON and the CSV, where
    other fields (LOINC, flag wording) may be written differently.
    """
    test_name = (lab.test_name or "").lower()
    return (lab.mrn, test_name, lab.collected_at, _comparable_value(lab.value))


def drop_duplicate_labs(labs: list[LabResult]) -> tuple[list[LabResult], int]:
    """Drop repeated lab results. Returns (kept, dropped_count)."""
    kept: list[LabResult] = []
    seen: dict[tuple, LabResult] = {}
    dropped = 0

    for lab in labs:
        key = lab_match_key(lab)
        if key not in seen:
            seen[key] = lab
            kept.append(lab)
            continue

        survivor = seen[key]
        if survivor.source_file == lab.source_file:
            rule = "DUPLICATE_DROPPED"
            reason = "Same lab result repeated in the same file. Dropped."
        else:
            rule = "CROSS_SOURCE_DUPLICATE"
            reason = (
                f"Same lab result (MRN, test, date, value) already loaded from "
                f"{survivor.source_file}. Dropped."
            )
        survivor.audit.extend(lab.audit)
        survivor.audit.append(make_entry(lab.id, "record", lab.id, survivor.id, rule, reason, "info"))
        dropped += 1

    return kept, dropped


# ---------------------------------------------------------------------------
# Identity conflicts
# ---------------------------------------------------------------------------


def _snapshot(patient: PatientRecord) -> dict[str, Any]:
    return {
        "id": patient.id,
        "mrn": patient.mrn,
        "given_name": patient.given_name,
        "family_name": patient.family_name,
        "birth_date": patient.birth_date.isoformat() if patient.birth_date else None,
        "gender": patient.gender,
        "source_ids": list(patient.source_ids),
    }


def _differing_fields(group: list[PatientRecord]) -> list[str]:
    differing = []
    for field in ["mrn", "given_name", "family_name", "birth_date"]:
        values = set()
        for patient in group:
            values.add(getattr(patient, field))
        if len(values) > 1:
            differing.append(field)
    return differing


def _flag(group: list[PatientRecord], rule: str, reason: str) -> None:
    for patient in group:
        patient.audit.append(
            make_entry(patient.id, "mrn", patient.mrn, patient.mrn, rule, reason, "warning")
        )


def detect_identity_conflicts(patients: list[PatientRecord]) -> list[Conflict]:
    """Find identity problems. Flags the records, never merges them."""
    conflicts: list[Conflict] = []

    # Same MRN, but different name or DOB.
    by_mrn: dict[str, list[PatientRecord]] = {}
    for patient in patients:
        by_mrn.setdefault(patient.mrn, []).append(patient)

    for mrn, group in by_mrn.items():
        if len(group) < 2:
            continue
        differing = _differing_fields(group)
        reason = (
            f"MRN {mrn} is used by {len(group)} records that differ in: "
            f"{', '.join(differing)}. Not merged; needs human review."
        )
        _flag(group, "MRN_CONFLICT", reason)
        conflicts.append(
            Conflict(
                conflict_type="MRN_CONFLICT",
                shared_value=mrn,
                differing_fields=differing,
                patient_ids=[p.id for p in group],
                records=[_snapshot(p) for p in group],
                reason=reason,
            )
        )

    # Same name and DOB, but different MRNs. Only checked when all three
    # are present, otherwise "both unknown" would look like a match.
    by_person: dict[tuple, list[PatientRecord]] = {}
    for patient in patients:
        if patient.given_name and patient.family_name and patient.birth_date:
            key = (patient.given_name.lower(), patient.family_name.lower(), patient.birth_date)
            by_person.setdefault(key, []).append(patient)

    for (given, family, birth_date), group in by_person.items():
        mrns = sorted({p.mrn for p in group})
        if len(mrns) < 2:
            continue
        shared = f"{group[0].given_name} {group[0].family_name} / {birth_date.isoformat()}"
        reason = (
            f"{shared} appears under different MRNs ({', '.join(mrns)}). Could be one "
            "patient with a typo, or two different people. Not merged; needs human review."
        )
        _flag(group, "POSSIBLE_DUPLICATE_PATIENT", reason)
        conflicts.append(
            Conflict(
                conflict_type="POSSIBLE_DUPLICATE_PATIENT",
                shared_value=shared,
                differing_fields=["mrn"],
                patient_ids=[p.id for p in group],
                records=[_snapshot(p) for p in group],
                reason=reason,
            )
        )

    return conflicts
