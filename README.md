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
