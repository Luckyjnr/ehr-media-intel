"""Task 2 command line: cleaned data -> FHIR bundles -> validation -> SQLite.

Run it with:
    python -m app.fhir.build

run_build() does the work in memory (easy to test).
main() reads data/clean/ and writes data/store/.
"""

import json
from pathlib import Path

from app.fhir.bundle import build_patient_bundle
from app.fhir.store import DEFAULT_DB_PATH, clear_bundles, save_bundle
from app.fhir.validate import validate_bundle, write_report
from app.ingestion.models import ClinicalDocument, Conflict, LabResult, PatientRecord

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLEAN_DIR = PROJECT_ROOT / "data" / "clean"
STORE_DIR = PROJECT_ROOT / "data" / "store"


def run_build(
    patients: list[PatientRecord],
    documents: list[ClinicalDocument],
    labs: list[LabResult],
    conflicts: list[Conflict],
) -> tuple[list[tuple[str, dict]], dict]:
    """Build and validate one bundle per patient.

    Returns (passed_bundles, report). passed_bundles is a list of (mrn, bundle).
    Bundles that fail validation are reported but not returned, so they are never stored.
    """
    # Patients whose MRN is shared by records that disagree on who they are.
    # We do not know which record the documents and labs belong to, so we
    # build nothing for that MRN until a human resolves it.
    held_for_review = []
    held_mrns = set()
    for conflict in conflicts:
        if conflict.conflict_type == "MRN_CONFLICT":
            held_mrns.add(conflict.shared_value)
            held_for_review.append(
                {
                    "mrn": conflict.shared_value,
                    "patient_ids": conflict.patient_ids,
                    "reason": conflict.reason,
                }
            )

    documents_by_mrn: dict[str, list[ClinicalDocument]] = {}
    for doc in documents:
        documents_by_mrn.setdefault(doc.mrn, []).append(doc)
    labs_by_mrn: dict[str, list[LabResult]] = {}
    for lab in labs:
        labs_by_mrn.setdefault(lab.mrn, []).append(lab)

    passed_bundles: list[tuple[str, dict]] = []
    bundle_reports = []
    known_mrns = set()

    for patient in patients:
        known_mrns.add(patient.mrn)
        if patient.mrn in held_mrns:
            continue

        bundle = build_patient_bundle(
            patient,
            documents_by_mrn.get(patient.mrn, []),
            labs_by_mrn.get(patient.mrn, []),
        )
        errors = validate_bundle(bundle)
        bundle_reports.append(
            {
                "mrn": patient.mrn,
                "bundle_id": bundle["id"],
                "resource_count": len(bundle["entry"]),
                "status": "passed" if not errors else "failed",
                "errors": errors,
            }
        )
        if not errors:
            passed_bundles.append((patient.mrn, bundle))

    # Documents or labs whose MRN has no patient record would otherwise
    # disappear silently, so we list them.
    unlinked_records = []
    for record in [*documents, *labs]:
        if record.mrn not in known_mrns:
            unlinked_records.append(record.id)

    passed_count = len(passed_bundles)
    report = {
        "summary": {
            "bundles_built": len(bundle_reports),
            "passed": passed_count,
            "failed": len(bundle_reports) - passed_count,
            "held_for_review": len(held_for_review),
        },
        "bundles": bundle_reports,
        "held_for_review": held_for_review,
        "unlinked_records": unlinked_records,
    }
    return passed_bundles, report


def _load(path: Path, model):
    data = json.loads(path.read_text(encoding="utf-8"))
    return [model.model_validate(item) for item in data]


def main() -> None:
    patients = _load(CLEAN_DIR / "patients.json", PatientRecord)
    documents = _load(CLEAN_DIR / "documents.json", ClinicalDocument)
    labs = _load(CLEAN_DIR / "labs.json", LabResult)
    conflicts = _load(CLEAN_DIR / "conflicts.json", Conflict)

    passed_bundles, report = run_build(patients, documents, labs, conflicts)

    # The bundles table is a derived output, so we rebuild it from scratch each run.
    # Otherwise a patient who is now held for review could keep an old bundle.
    # Only this table is cleared: the same file also holds the Task 3 summary cache.
    clear_bundles()
    for mrn, bundle in passed_bundles:
        save_bundle(mrn, bundle)

    write_report(report, STORE_DIR / "validation_report.json", STORE_DIR / "validation_report.md")

    summary = report["summary"]
    resource_total = 0
    for bundle_report in report["bundles"]:
        if bundle_report["status"] == "passed":
            resource_total += bundle_report["resource_count"]

    print("\n=== Task 2: FHIR build summary ===\n")
    print(f"Bundles built:     {summary['bundles_built']}")
    print(f"Passed:            {summary['passed']}  ({resource_total} resources stored)")
    print(f"Failed:            {summary['failed']}")
    print(f"Held for review:   {summary['held_for_review']} MRN(s) with an MRN conflict")
    for held in report["held_for_review"]:
        print(f"                   - MRN {held['mrn']} ({len(held['patient_ids'])} records)")
    if report["unlinked_records"]:
        print(f"Unlinked records:  {len(report['unlinked_records'])}")
    print(f"\nDatabase: {DEFAULT_DB_PATH}")
    print(f"Report:   {STORE_DIR / 'validation_report.md'}\n")


if __name__ == "__main__":
    main()
