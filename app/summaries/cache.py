"""Cache summaries in SQLite (the "summaries" table in data/store/fhir.db).

Key = MRN + record_hash. The hash covers everything that would change the
summary: the bundle itself, the prompt version and the model. If any of them
changes, the hash changes and a new summary is made. If none change, we reuse
the saved summary and make no API call (saves cost and gives stable output).
"""

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path

from app.fhir.store import DEFAULT_DB_PATH
from app.summaries.models import ClinicalSummary
from app.summaries.prompt import PROMPT_VERSION

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS summaries (
    mrn TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (mrn, record_hash)
)
"""


def record_hash(bundle: dict, model: str) -> str:
    # sort_keys so the same bundle always gives the same text, and so the same hash.
    bundle_text = json.dumps(bundle, sort_keys=True)
    combined = f"{bundle_text}|{PROMPT_VERSION}|{model}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute(CREATE_TABLE_SQL)
    return connection


def get_cached_summary(mrn: str, hash_value: str, db_path: Path = DEFAULT_DB_PATH) -> ClinicalSummary | None:
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT summary_json FROM summaries WHERE mrn = ? AND record_hash = ?",
            (mrn, hash_value),
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        return None
    return ClinicalSummary.model_validate_json(row[0])


def save_summary(summary: ClinicalSummary, hash_value: str, db_path: Path = DEFAULT_DB_PATH) -> None:
    connection = _connect(db_path)
    try:
        with connection:  # commits on success, rolls back on error
            connection.execute(
                "INSERT OR REPLACE INTO summaries (mrn, record_hash, summary_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    summary.mrn,
                    hash_value,
                    summary.model_dump_json(),
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                ),
            )
    finally:
        connection.close()
