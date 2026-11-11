"""Store bundles in a local SQLite database (data/store/fhir.db).

One row per patient, keyed by MRN. The whole bundle is stored as JSON text,
because we always read a patient's bundle as one piece.
SQLite needs no server, which is enough for a single-machine prototype.
"""

import datetime as dt
import json
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "store" / "fhir.db"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bundles (
    mrn TEXT PRIMARY KEY,
    bundle_json TEXT NOT NULL,
    resource_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
)
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute(CREATE_TABLE_SQL)
    return connection


def save_bundle(mrn: str, bundle: dict, db_path: Path = DEFAULT_DB_PATH) -> None:
    """Insert, or replace the existing bundle for this MRN (so re-runs don't duplicate)."""
    connection = _connect(db_path)
    try:
        # "with connection" commits on success and rolls back on error.
        with connection:
            connection.execute(
                "INSERT OR REPLACE INTO bundles (mrn, bundle_json, resource_count, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    mrn,
                    json.dumps(bundle),
                    len(bundle.get("entry", [])),
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                ),
            )
    finally:
        connection.close()


def get_bundle(mrn: str, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    """The stored bundle for this MRN, or None if there is none."""
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT bundle_json FROM bundles WHERE mrn = ?", (mrn,)
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        return None
    return json.loads(row[0])


def list_bundles(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """A short listing (no bundle content): mrn, resource_count, created_at."""
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            "SELECT mrn, resource_count, created_at FROM bundles ORDER BY mrn"
        ).fetchall()
    finally:
        connection.close()

    result = []
    for mrn, resource_count, created_at in rows:
        result.append({"mrn": mrn, "resource_count": resource_count, "created_at": created_at})
    return result
