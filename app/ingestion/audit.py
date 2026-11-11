"""Audit trail for the cleaning pipeline.

In healthcare we must be able to explain every change made to a record.
So each cleaning step returns AuditEntry objects instead of changing data silently.
Each cleaned record keeps its own list of entries, and the pipeline also writes
them all to one audit_log.jsonl file.
"""

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

# info    = we changed the format, but the meaning is the same (e.g. date reformatted)
# warning = we made a judgement call or something is missing; a human may want to check
# error   = the value was unusable and we threw it away (or rejected the whole record)
Severity = Literal["info", "warning", "error"]


class AuditEntry(BaseModel):
    record_id: str  # where the record came from, e.g. "lab_results.csv:line4#lab"
    field: str  # which field changed, e.g. "birth_date"
    old_value: str | None = None  # the raw value as it arrived
    new_value: str | None = None  # the value we stored after cleaning
    rule: str  # short code, e.g. "DATE_NORMALIZED"
    reason: str  # plain-English explanation for a human reviewer
    severity: Severity


def _to_text(value: Any) -> str | None:
    # Audit values are stored as text so a date, a number and a string
    # all look the same in the log file.
    if value is None:
        return None
    return str(value)


def make_entry(
    record_id: str,
    field: str,
    old_value: Any,
    new_value: Any,
    rule: str,
    reason: str,
    severity: Severity = "info",
) -> AuditEntry:
    """Small helper so callers don't have to convert values to text themselves."""
    return AuditEntry(
        record_id=record_id,
        field=field,
        old_value=_to_text(old_value),
        new_value=_to_text(new_value),
        rule=rule,
        reason=reason,
        severity=severity,
    )


def count_by_severity(entries: list[AuditEntry]) -> dict[str, int]:
    counts = {"info": 0, "warning": 0, "error": 0}
    for entry in entries:
        counts[entry.severity] += 1
    return counts


def write_audit_log(entries: list[AuditEntry], path: Path) -> None:
    """Write one JSON object per line (JSONL).

    JSONL is easy to append to, and easy to grep or load line by line,
    which suits a log that can grow large.
    """
    with path.open("w", encoding="utf-8") as file:
        for entry in entries:
            file.write(entry.model_dump_json() + "\n")
