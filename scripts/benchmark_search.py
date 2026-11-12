"""Speed check for semantic search.

Run it with:
    python scripts/benchmark_search.py

Builds a TEMPORARY index (the real one in data/store/chroma is not touched)
with our real records plus synthetic filler items to reach at least 50,
then times 20 different searches. Model load time is reported separately,
because the API loads the model once at startup, not per search.
"""

import statistics
import sys
import tempfile
import time
from pathlib import Path

# Let this script import the "app" package when run as a plain file.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.search.index import add_items, collect_items, load_embedder, open_collection  # noqa: E402
from app.search.searcher import search  # noqa: E402

MIN_ITEMS = 50
TARGET_SECONDS = 2.0

QUERIES = [
    "patients with high blood sugar",
    "brain scan for memory problems",
    "heart failure with fluid overload",
    "low hemoglobin anemia",
    "kidney disease with high potassium",
    "lung nodule on CT scan",
    "thyroid nodule biopsy",
    "pneumonia treated with antibiotics",
    "chest pain ruled out heart attack",
    "knee arthritis",
    "abnormal lab results",
    "vitamin deficiency",
    "diabetes medication changes",
    "normal echocardiogram",
    "abdominal pain CT scan",
    "high cholesterol statin",
    "emergency department visit",
    "follow up imaging recommended",
    "elevated BNP",
    "discharge instructions",
]

# Clearly fake filler (MRN 99xxxxx) to bring the index to a realistic minimum size.
FILLER_TOPICS = [
    "Routine annual physical, no acute complaints, vitals stable.",
    "Seasonal allergies, started antihistamine, follow up as needed.",
    "Ankle sprain after a fall, rest, ice and compression advised.",
    "Migraine headaches twice a month, started a preventive medication.",
    "Hypertension follow up, blood pressure improved on current dose.",
    "Asthma check, inhaler technique reviewed, no recent attacks.",
    "Lower back pain, physical therapy referral, no red flags.",
    "Skin rash on both arms, likely contact dermatitis, topical steroid.",
    "Insomnia, sleep hygiene counseling, short course of medication.",
    "Iron studies show low ferritin, oral iron started.",
]


def filler_items(count: int) -> list[dict]:
    items = []
    for number in range(count):
        text = FILLER_TOPICS[number % len(FILLER_TOPICS)]
        mrn = f"99{number:05d}"
        date = f"2023-{(number % 12) + 1:02d}-15"
        items.append(
            {
                "id": f"filler-{number}",
                "text": text,
                "metadata": {
                    "mrn": mrn,
                    "patient_name": "Synthetic Filler",
                    "resource_type": "DocumentReference",
                    "resource_id": f"filler-{number}",
                    "date": date,
                    "date_int": int(date.replace("-", "")),
                    "snippet": text,
                    "summary_snippet": "",
                },
            }
        )
    return items


def main() -> None:
    started = time.perf_counter()
    embed = load_embedder()
    load_seconds = time.perf_counter() - started

    items = collect_items()
    real_count = len(items)
    if real_count < MIN_ITEMS:
        items += filler_items(MIN_ITEMS - real_count + 10)

    with tempfile.TemporaryDirectory() as temp_dir:
        collection = open_collection(Path(temp_dir), reset=True)
        add_items(collection, items, embed)

        search(QUERIES[0], embed, collection)  # warm-up, not counted

        timings = []
        for query in QUERIES:
            query_started = time.perf_counter()
            search(query, embed, collection)
            timings.append(time.perf_counter() - query_started)

    average_ms = statistics.mean(timings) * 1000
    slowest_ms = max(timings) * 1000
    passed = max(timings) < TARGET_SECONDS

    print("\n=== Search benchmark ===\n")
    print(f"Model load (once, at startup): {load_seconds:.1f} s")
    print(f"Items indexed:                 {len(items)} ({real_count} real, {len(items) - real_count} synthetic filler)")
    print(f"Searches timed:                {len(timings)}")
    print(f"Average per search:            {average_ms:.1f} ms")
    print(f"Slowest search:                {slowest_ms:.1f} ms")
    print(f"Target (< {TARGET_SECONDS:.0f} s per search):     {'PASS' if passed else 'FAIL'}\n")


if __name__ == "__main__":
    main()
