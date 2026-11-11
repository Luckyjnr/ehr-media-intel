"""Read the three raw sources into plain Python dicts.

Loaders do NOT clean anything. They only rename fields to one common shape,
so the cleaning code does not need to know which file a record came from.

Each raw dict gets:
  source_id   - exact location in the source (used as record id and in the audit log)
  source_file - which file it came from

Raw patient dicts have either given_name/family_name (EHR) or full_name (CSV, notes).
Only the EHR has a gender column, so only EHR patient dicts have a "gender" key.
"""

import csv
import json
from pathlib import Path
from typing import Any

RawRecord = dict[str, Any]


def load_ehr_export(path: Path) -> tuple[list[RawRecord], list[RawRecord], list[RawRecord]]:
    """Read the Epic-style JSON export. Returns (patients, documents, labs).

    Documents and labs are nested inside encounters, inside patients,
    so we flatten them and copy the patient's MRN onto each one.
    """
    with path.open(encoding="utf-8") as file:
        data = json.load(file)

    source_file = path.name
    patients: list[RawRecord] = []
    documents: list[RawRecord] = []
    labs: list[RawRecord] = []

    for patient_index, patient in enumerate(data.get("patients", [])):
        # Position-based ids stay unique even when the export repeats a record.
        patient_id = f"{source_file}:patient[{patient_index}]"
        name = patient.get("name") or {}

        patients.append(
            {
                "source_id": patient_id,
                "source_file": source_file,
                "mrn": patient.get("mrn"),
                "given_name": name.get("first"),
                "family_name": name.get("last"),
                "birth_date": patient.get("dob"),
                "gender": patient.get("sex"),
            }
        )

        for encounter_index, encounter in enumerate(patient.get("encounters", [])):
            encounter_path = f"{patient_id}/encounter[{encounter_index}]"

            for doc_index, doc in enumerate(encounter.get("documents", [])):
                documents.append(
                    {
                        "source_id": f"{encounter_path}/document[{doc_index}]",
                        "source_file": source_file,
                        "mrn": patient.get("mrn"),
                        "doc_type": doc.get("type"),
                        "date": doc.get("date"),
                        "title": doc.get("title"),
                        "text": doc.get("text"),
                        "encounter_id": encounter.get("encounter_id"),
                    }
                )

            for lab_index, lab in enumerate(encounter.get("labs", [])):
                labs.append(
                    {
                        "source_id": f"{encounter_path}/lab[{lab_index}]",
                        "source_file": source_file,
                        "mrn": patient.get("mrn"),
                        "test_name": lab.get("test"),
                        "loinc_code": lab.get("loinc"),
                        "value": lab.get("value"),
                        "unit": lab.get("unit"),
                        "reference_range": lab.get("ref_range"),
                        "flag": lab.get("flag"),
                        "collected_at": lab.get("collected"),
                    }
                )

    return patients, documents, labs


def load_lab_csv(path: Path) -> tuple[list[RawRecord], list[RawRecord]]:
    """Read the lab CSV. Returns (patient_mentions, labs).

    Every row describes a lab AND the patient it belongs to, so each row
    produces one of each. The patient part is used to cross-check identity.
    """
    source_file = path.name
    patients: list[RawRecord] = []
    labs: list[RawRecord] = []

    with path.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        # start=2 because line 1 of the file is the header row.
        for line_number, row in enumerate(reader, start=2):
            row_id = f"{source_file}:line{line_number}"

            patients.append(
                {
                    "source_id": f"{row_id}#patient",
                    "source_file": source_file,
                    "mrn": row.get("mrn"),
                    "full_name": row.get("patient_name"),
                    "birth_date": row.get("dob"),
                }
            )
            labs.append(
                {
                    "source_id": f"{row_id}#lab",
                    "source_file": source_file,
                    "mrn": row.get("mrn"),
                    "test_name": row.get("test_name"),
                    "loinc_code": row.get("loinc_code"),
                    "value": row.get("value"),
                    "unit": row.get("unit"),
                    "reference_range": row.get("reference_range"),
                    "flag": row.get("flag"),
                    "collected_at": row.get("collected_date"),
                }
            )

    return patients, labs


def parse_note(text: str) -> tuple[dict[str, str], str]:
    """Split a scanned note into (header fields, body text).

    The header is the "Key: value" lines at the top, ending at the first blank line.
    Keys are lower-cased so "MRN:" and "mrn:" are the same.
    """
    header: dict[str, str] = {}
    lines = text.splitlines()
    body_start = len(lines)

    for index, line in enumerate(lines):
        if line.strip() == "":
            body_start = index + 1
            break
        if ":" not in line:
            # Not a header line, so the body starts here.
            body_start = index
            break
        key, value = line.split(":", 1)
        header[key.strip().lower()] = value.strip()

    body = "\n".join(lines[body_start:]).strip()
    return header, body


def load_notes(folder: Path) -> tuple[list[RawRecord], list[RawRecord]]:
    """Read every .txt note. Returns (patient_mentions, documents)."""
    patients: list[RawRecord] = []
    documents: list[RawRecord] = []

    # sorted() so the output order is the same on every machine.
    for path in sorted(folder.glob("*.txt")):
        header, body = parse_note(path.read_text(encoding="utf-8"))
        source_file = f"notes/{path.name}"

        patients.append(
            {
                "source_id": f"{source_file}#patient",
                "source_file": source_file,
                "mrn": header.get("mrn"),
                "full_name": header.get("patient"),
                "birth_date": header.get("dob"),
            }
        )
        documents.append(
            {
                "source_id": f"{source_file}#document",
                "source_file": source_file,
                "mrn": header.get("mrn"),
                "doc_type": header.get("type"),
                "date": header.get("date"),
                "title": header.get("title") or header.get("type"),
                "text": body,
                "encounter_id": None,
            }
        )

    return patients, documents
