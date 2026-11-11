# EHR Media Intelligence Platform

Take-home assessment. All data in `data/raw/` is **100% synthetic**.

## Setup

Requires Python 3.11+.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Task 1: data ingestion and cleaning

```bash
python -m app.ingestion.pipeline   # read data/raw, write data/clean, print a summary
pytest -v                          # unit tests (they do not read data/raw)
```

Inputs (`data/raw/`, problems listed in `data/raw/README.md`):
- `ehr_export.json`: Epic-style export with nested encounters, documents and labs
- `lab_results.csv`: lab rows with patient name and DOB
- `notes/*.txt`: scanned notes with a loose `Key: value` header

Outputs (`data/clean/`, git-ignored):

| File | Contents |
|---|---|
| `patients.json`, `documents.json`, `labs.json` | Cleaned records, each with its own `audit` list |
| `audit_log.jsonl` | Every change, one JSON object per line |
| `rejected.json` | Records we could not keep safely (e.g. no MRN), with the raw data |
| `conflicts.json` | Patient identity problems for human review (never auto-merged) |

Code lives in `app/ingestion/`: `loaders` → `cleaners` → `dedupe` → `pipeline`, with
models in `models.py` and the audit trail in `audit.py`.

## Task 2: FHIR mapping, validation and storage

```bash
python -m app.ingestion.pipeline   # Task 1 first: writes data/clean/
python -m app.fhir.build           # read data/clean, write data/store, print a summary
```

- Maps cleaned records to FHIR R4 resources (via `fhir.resources.R4B`): `Patient`, `Encounter`,
  `DocumentReference` (note text as base64 `text/plain`) and `DiagnosticReport` (one per lab).
- Builds one `Bundle` (type `collection`) per patient; resources reference each other with `urn:uuid` fullUrls.
- Patients in an `MRN_CONFLICT` get no bundle and are listed as "held for review".
- Validates every resource through `fhir.resources` and checks every reference resolves inside its bundle.
  Only bundles that pass are stored.

Outputs (`data/store/`, git-ignored):

| File | Contents |
|---|---|
| `fhir.db` | SQLite table `bundles (mrn, bundle_json, resource_count, created_at)` |
| `validation_report.json` / `.md` | Bundles built, passed, failed, held for review, errors in plain words |

Code lives in `app/fhir/`: `mapper` → `bundle` → `validate` → `store`, run by `build`.
