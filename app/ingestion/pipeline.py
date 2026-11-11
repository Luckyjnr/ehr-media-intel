"""Task 1 pipeline: load -> clean -> remove duplicates -> detect conflicts -> write.

Run it with:
    python -m app.ingestion.pipeline

run_pipeline() does all the work in memory and never touches files,
so tests can call it with small hand-made inputs.
main() is the thin part that reads the raw files and writes the outputs.
"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.ingestion.audit import AuditEntry, count_by_severity, write_audit_log
from app.ingestion.cleaners import (
    clean_text,
    missing_field_entry,
    normalize_date,
    normalize_doc_type,
    normalize_flag,
    normalize_gender,
    normalize_mrn,
    normalize_name,
    split_full_name,
)
from app.ingestion.dedupe import (
    build_patient_index,
    detect_identity_conflicts,
    drop_duplicate_documents,
    drop_duplicate_labs,
)
from app.ingestion.loaders import RawRecord, load_ehr_export, load_lab_csv, load_notes
from app.ingestion.models import ClinicalDocument, Conflict, LabResult, PatientRecord, RejectedRecord

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
CLEAN_DIR = PROJECT_ROOT / "data" / "clean"


# ---------------------------------------------------------------------------
# Cleaning one record at a time
# ---------------------------------------------------------------------------


def _reject(raw: RawRecord, record_type: str, audit: list[AuditEntry]) -> RejectedRecord:
    # The last audit entry is the MRN error that caused the rejection.
    return RejectedRecord(
        record_id=raw["source_id"],
        record_type=record_type,
        source_file=raw["source_file"],
        reason=audit[-1].reason,
        raw=raw,
        audit=audit,
    )


def _optional_text(raw: RawRecord, key: str, audit: list[AuditEntry]) -> str | None:
    """Read an optional text field. If it is missing, log MISSING_FIELD."""
    value = clean_text(raw.get(key))
    if value is None:
        audit.append(missing_field_entry(raw["source_id"], key))
    return value


def clean_patient(raw: RawRecord, today: dt.date | None = None) -> PatientRecord | RejectedRecord:
    record_id = raw["source_id"]
    audit: list[AuditEntry] = []

    mrn, entries = normalize_mrn(raw.get("mrn"), record_id)
    audit.extend(entries)
    if mrn is None:
        return _reject(raw, "patient", audit)

    # The CSV and notes give one name string; the EHR gives first and last separately.
    if "full_name" in raw:
        given_raw, family_raw = split_full_name(raw.get("full_name"))
    else:
        given_raw = raw.get("given_name")
        family_raw = raw.get("family_name")

    given_name, entries = normalize_name(given_raw, record_id, "given_name")
    audit.extend(entries)
    family_name, entries = normalize_name(family_raw, record_id, "family_name")
    audit.extend(entries)

    birth_date, entries = normalize_date(raw.get("birth_date"), record_id, "birth_date", today)
    audit.extend(entries)

    # Only the EHR export has a gender column. If the source format has no
    # gender at all, "unknown" is expected and not a data-quality problem,
    # so we only log MISSING_FIELD when the column exists but is empty.
    if "gender" in raw:
        gender, entries = normalize_gender(raw.get("gender"), record_id)
        audit.extend(entries)
    else:
        gender = "unknown"

    return PatientRecord(
        id=record_id,
        mrn=mrn,
        given_name=given_name,
        family_name=family_name,
        birth_date=birth_date,
        gender=gender,
        source_ids=[record_id],
        audit=audit,
    )


def clean_document(raw: RawRecord, today: dt.date | None = None) -> ClinicalDocument | RejectedRecord:
    record_id = raw["source_id"]
    audit: list[AuditEntry] = []

    mrn, entries = normalize_mrn(raw.get("mrn"), record_id)
    audit.extend(entries)
    if mrn is None:
        return _reject(raw, "document", audit)

    doc_type, entries = normalize_doc_type(raw.get("doc_type"), record_id)
    audit.extend(entries)
    doc_date, entries = normalize_date(raw.get("date"), record_id, "date", today)
    audit.extend(entries)

    title = _optional_text(raw, "title", audit)
    text = _optional_text(raw, "text", audit)

    # Not logged when missing: scanned notes never have an encounter id.
    encounter_id = clean_text(raw.get("encounter_id"))

    return ClinicalDocument(
        id=record_id,
        mrn=mrn,
        doc_type=doc_type,
        date=doc_date,
        title=title,
        text=text or "",
        encounter_id=encounter_id,
        source_file=raw["source_file"],
        audit=audit,
    )


def clean_lab(raw: RawRecord, today: dt.date | None = None) -> LabResult | RejectedRecord:
    record_id = raw["source_id"]
    audit: list[AuditEntry] = []

    mrn, entries = normalize_mrn(raw.get("mrn"), record_id)
    audit.extend(entries)
    if mrn is None:
        return _reject(raw, "lab", audit)

    test_name = _optional_text(raw, "test_name", audit)
    loinc_code = _optional_text(raw, "loinc_code", audit)
    value = _optional_text(raw, "value", audit)
    unit = _optional_text(raw, "unit", audit)
    reference_range = _optional_text(raw, "reference_range", audit)

    flag, entries = normalize_flag(raw.get("flag"), record_id)
    audit.extend(entries)
    collected_at, entries = normalize_date(raw.get("collected_at"), record_id, "collected_at", today)
    audit.extend(entries)

    return LabResult(
        id=record_id,
        mrn=mrn,
        test_name=test_name,
        loinc_code=loinc_code,
        value=value,
        unit=unit,
        reference_range=reference_range,
        flag=flag,
        collected_at=collected_at,
        source_file=raw["source_file"],
        audit=audit,
    )


# ---------------------------------------------------------------------------
# The whole pipeline (no file access)
# ---------------------------------------------------------------------------


class PipelineResult(BaseModel):
    patients: list[PatientRecord]
    documents: list[ClinicalDocument]
    labs: list[LabResult]
    rejected: list[RejectedRecord]
    conflicts: list[Conflict]
    duplicates_dropped: dict[str, int]
    patient_mentions_linked: int

    def all_audit_entries(self) -> list[AuditEntry]:
        """Every audit entry, from kept and rejected records.

        Dropped duplicates handed their entries to the record that was kept,
        so nothing is lost here.
        """
        entries: list[AuditEntry] = []
        for patient in self.patients:
            entries.extend(patient.audit)
        for doc in self.documents:
            entries.extend(doc.audit)
        for lab in self.labs:
            entries.extend(lab.audit)
        for rejected in self.rejected:
            entries.extend(rejected.audit)
        return entries


def run_pipeline(
    raw_patients: list[RawRecord],
    raw_documents: list[RawRecord],
    raw_labs: list[RawRecord],
    today: dt.date | None = None,
) -> PipelineResult:
    """Clean everything, then remove duplicates, then look for identity conflicts.

    Pass raw_patients with EHR records first: the first record seen for a
    patient is the one we keep, and the EHR is the most trusted source.
    """
    rejected: list[RejectedRecord] = []

    patient_mentions: list[PatientRecord] = []
    for raw in raw_patients:
        result = clean_patient(raw, today)
        if isinstance(result, RejectedRecord):
            rejected.append(result)
        else:
            patient_mentions.append(result)

    documents: list[ClinicalDocument] = []
    for raw in raw_documents:
        result = clean_document(raw, today)
        if isinstance(result, RejectedRecord):
            rejected.append(result)
        else:
            documents.append(result)

    labs: list[LabResult] = []
    for raw in raw_labs:
        result = clean_lab(raw, today)
        if isinstance(result, RejectedRecord):
            rejected.append(result)
        else:
            labs.append(result)

    # Duplicates are removed after cleaning, because "MRN-0012345" and "12345"
    # only look the same once they are both "0012345".
    patients, patient_duplicates, linked = build_patient_index(patient_mentions)
    documents, document_duplicates = drop_duplicate_documents(documents)
    labs, lab_duplicates = drop_duplicate_labs(labs)

    conflicts = detect_identity_conflicts(patients)

    return PipelineResult(
        patients=patients,
        documents=documents,
        labs=labs,
        rejected=rejected,
        conflicts=conflicts,
        duplicates_dropped={
            "patients": patient_duplicates,
            "documents": document_duplicates,
            "labs": lab_duplicates,
        },
        patient_mentions_linked=linked,
    )


# ---------------------------------------------------------------------------
# File output and command line
# ---------------------------------------------------------------------------


def _write_json(path: Path, items: list[BaseModel]) -> None:
    # mode="json" turns dates into "YYYY-MM-DD" strings.
    data = [item.model_dump(mode="json") for item in items]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def write_outputs(result: PipelineResult, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "patients.json", result.patients)
    _write_json(out_dir / "documents.json", result.documents)
    _write_json(out_dir / "labs.json", result.labs)
    _write_json(out_dir / "rejected.json", result.rejected)
    _write_json(out_dir / "conflicts.json", result.conflicts)
    write_audit_log(result.all_audit_entries(), out_dir / "audit_log.jsonl")


def print_summary(read_counts: dict[str, dict[str, int]], result: PipelineResult, out_dir: Path) -> None:
    print("\n=== Task 1: ingestion summary ===\n")

    print("Records read per source:")
    for source, counts in read_counts.items():
        parts = []
        for kind, number in counts.items():
            parts.append(f"{number} {kind}")
        print(f"  {source:<18} {', '.join(parts)}")

    print("\nCleaned (kept):")
    print(f"  patients   {len(result.patients)}")
    print(f"  documents  {len(result.documents)}")
    print(f"  labs       {len(result.labs)}")

    rejected_by_type: dict[str, int] = {}
    for record in result.rejected:
        rejected_by_type[record.record_type] = rejected_by_type.get(record.record_type, 0) + 1
    print(f"\nRejected: {len(result.rejected)} {rejected_by_type}")

    dupes = result.duplicates_dropped
    print(f"\nDuplicates dropped: {sum(dupes.values())} {dupes}")
    print(f"Patient mentions linked to an existing patient: {result.patient_mentions_linked}")

    conflict_counts: dict[str, int] = {}
    for conflict in result.conflicts:
        conflict_counts[conflict.conflict_type] = conflict_counts.get(conflict.conflict_type, 0) + 1
    print(f"\nConflicts found: {len(result.conflicts)} {conflict_counts}")

    entries = result.all_audit_entries()
    print(f"\nAudit entries: {len(entries)} {count_by_severity(entries)}")
    print(f"\nOutputs written to {out_dir}\n")


def main() -> None:
    ehr_patients, ehr_documents, ehr_labs = load_ehr_export(RAW_DIR / "ehr_export.json")
    csv_patients, csv_labs = load_lab_csv(RAW_DIR / "lab_results.csv")
    note_patients, note_documents = load_notes(RAW_DIR / "notes")

    read_counts: dict[str, dict[str, Any]] = {
        "ehr_export.json": {
            "patients": len(ehr_patients),
            "documents": len(ehr_documents),
            "labs": len(ehr_labs),
        },
        "lab_results.csv": {"rows": len(csv_labs)},
        "notes/*.txt": {"notes": len(note_documents)},
    }

    result = run_pipeline(
        # EHR first: it is the source of truth for demographics.
        raw_patients=ehr_patients + csv_patients + note_patients,
        raw_documents=ehr_documents + note_documents,
        raw_labs=ehr_labs + csv_labs,
    )
    write_outputs(result, CLEAN_DIR)
    print_summary(read_counts, result, CLEAN_DIR)


if __name__ == "__main__":
    main()
