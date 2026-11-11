"""Canonical (cleaned) data models.

Every source (JSON export, CSV, scanned notes) is converted into these shapes.
Later tasks (FHIR, search, AI) only ever read these models, never the raw files.
"""

# Imported as "dt" because ClinicalDocument has a field called "date",
# and a field with the same name as its type confuses Pydantic.
import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.ingestion.audit import AuditEntry

# Same four values as FHIR "administrative-gender", so the FHIR step can copy it as is.
Gender = Literal["male", "female", "other", "unknown"]

DocType = Literal["discharge_summary", "imaging", "lab_report", "clinical_note", "other"]

LabFlag = Literal["normal", "high", "low", "critical"]


class PatientRecord(BaseModel):
    # id is the source location of the first record we saw for this patient.
    # It is not a medical identifier, it only lets us point back to the raw data.
    id: str
    mrn: str  # always 7 digits as a string, e.g. "0012345"
    given_name: str | None = None
    family_name: str | None = None
    birth_date: dt.date | None = None
    gender: Gender = "unknown"
    # Every raw record that described this same patient (EHR row, CSV rows, notes).
    source_ids: list[str] = Field(default_factory=list)
    audit: list[AuditEntry] = Field(default_factory=list)


class ClinicalDocument(BaseModel):
    id: str
    mrn: str
    doc_type: DocType
    date: dt.date | None = None
    title: str | None = None
    text: str = ""
    encounter_id: str | None = None
    source_file: str
    audit: list[AuditEntry] = Field(default_factory=list)


class LabResult(BaseModel):
    id: str
    mrn: str
    test_name: str | None = None
    loinc_code: str | None = None
    # Kept as text because lab values are not always numbers ("positive", "<0.5").
    value: str | None = None
    unit: str | None = None
    reference_range: str | None = None
    flag: LabFlag | None = None
    collected_at: dt.date | None = None
    source_file: str
    audit: list[AuditEntry] = Field(default_factory=list)


class RejectedRecord(BaseModel):
    """A record we could not safely keep (for example: no MRN).

    We keep the raw data so a human can fix it at the source and re-run.
    """

    record_id: str
    record_type: Literal["patient", "document", "lab"]
    source_file: str
    reason: str
    raw: dict[str, Any]
    audit: list[AuditEntry] = Field(default_factory=list)


class Conflict(BaseModel):
    """A possible patient identity problem that needs a human decision."""

    conflict_type: Literal["MRN_CONFLICT", "POSSIBLE_DUPLICATE_PATIENT"]
    # The value the records share: the MRN, or "Name / DOB" for possible duplicates.
    shared_value: str
    differing_fields: list[str]
    patient_ids: list[str]
    records: list[dict[str, Any]]  # a small snapshot of each record involved
    reason: str
