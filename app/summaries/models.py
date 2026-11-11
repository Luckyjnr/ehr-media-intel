"""Shapes for AI summaries.

ModelDraft is what we ask Claude to write. ClinicalSummary is what we store:
the draft plus fields that OUR code fills in (identity, disclaimer, model,
timestamps, quality checks). The model never writes the MRN, name or
disclaimer, so it cannot get them wrong.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

Confidence = Literal["low", "medium", "high"]
CheckResult = Literal["pass", "fail"]


class MediaItem(BaseModel):
    type: str  # e.g. "Discharge summary", "Imaging report"
    date: str  # ISO date, or "Not documented"
    note: str  # one short line about the document


class ModelDraft(BaseModel):
    """The JSON the model must return. Extra keys (e.g. a disclaimer) are ignored."""

    model_config = ConfigDict(extra="ignore")

    chief_concern: str
    key_diagnoses: list[str]
    recent_media: list[MediaItem]
    flagged_anomalies: list[str]
    summary_text: str
    confidence: Confidence
    confidence_reason: str


class ClinicalSummary(ModelDraft):
    mrn: str
    patient_name: str
    disclaimer: str
    model: str  # model id, or "mock"
    prompt_version: str
    generated_at: str  # ISO timestamp, UTC
    quality_checks: dict[str, CheckResult] = {}
