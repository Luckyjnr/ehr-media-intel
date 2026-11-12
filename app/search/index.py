"""Build the search index: bundles + AI summaries -> embeddings -> ChromaDB.

Run it with:
    python -m app.search.index

"Semantic" search means we compare meaning, not exact words. Each text is
turned into a vector (a list of numbers) by a small local model. Texts with
similar meaning get vectors that point in similar directions, so
"high blood sugar" can match a note that says "hyperglycemia".
"""

import base64
import os
from pathlib import Path
from typing import Callable

import chromadb
from chromadb.config import Settings
from dotenv import load_dotenv

from app.fhir.store import DEFAULT_DB_PATH, get_bundle, list_bundles
from app.summaries.cache import get_cached_summary, record_hash
from app.summaries.models import ClinicalSummary
from app.summaries.prompt import extract_facts
from app.summaries.summarizer import DEFAULT_MODEL, MOCK_MODEL

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHROMA_PATH = PROJECT_ROOT / "data" / "store" / "chroma"
COLLECTION_NAME = "ehr_search"

# Small (~90 MB), fast, runs on a laptop CPU, no API key. Downloaded once, then cached.
# Note: it reads at most ~256 word pieces per text; our documents are shorter than that.
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
SNIPPET_CHARS = 200

# An embedder takes a list of texts and returns one vector per text.
# Passing it in (instead of loading it inside) lets tests use a fake one.
Embedder = Callable[[list[str]], list[list[float]]]


def load_embedder() -> Embedder:
    """Load the real model. Slow the first time (download), then a few seconds."""
    # Imported here because it pulls in PyTorch, which is slow to import.
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(EMBEDDING_MODEL)

    def embed(texts: list[str]) -> list[list[float]]:
        vectors = model.encode(texts, normalize_embeddings=True)
        return vectors.tolist()

    return embed


def open_collection(chroma_path: Path = CHROMA_PATH, reset: bool = False):
    """Open the ChromaDB collection on disk. reset=True deletes it first."""
    client = chromadb.PersistentClient(
        path=str(chroma_path),
        settings=Settings(anonymized_telemetry=False),
    )
    if reset:
        existing = [collection.name for collection in client.list_collections()]
        if COLLECTION_NAME in existing:
            client.delete_collection(COLLECTION_NAME)
    return client.get_or_create_collection(
        COLLECTION_NAME,
        # Cosine distance compares the direction of vectors, which is what
        # carries meaning for this model. 0 = same meaning, 2 = opposite.
        configuration={"hnsw": {"space": "cosine"}},
        # We create the vectors ourselves, so Chroma must not embed anything.
        embedding_function=None,
    )


def date_to_int(iso_date: str | None) -> int:
    """'2024-03-05' -> 20240305. Missing -> 0.

    Chroma can only compare numbers with $gte/$lte, not date strings.
    0 means "no date": it falls outside every date range, which is correct,
    because we cannot say an undated item is inside the range.
    """
    if not iso_date:
        return 0
    return int(iso_date[:10].replace("-", ""))


def make_snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """First ~limit characters, cut at a word boundary."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "..."


def current_summary(mrn: str, bundle: dict, db_path: Path = DEFAULT_DB_PATH) -> ClinicalSummary | None:
    """The cached AI summary that matches this exact bundle, or None.

    The cache key includes the bundle hash, so this never returns a summary
    written for an older version of the record. A real model summary is
    preferred; a mock-mode summary is the fallback.
    """
    real_model = os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
    for model in [real_model, MOCK_MODEL]:
        summary = get_cached_summary(mrn, record_hash(bundle, model), db_path)
        if summary is not None:
            return summary
    return None


def bundle_items(bundle: dict, summary: ClinicalSummary | None) -> list[dict]:
    """One search item per document, per lab report, and one for the summary."""
    facts = extract_facts(bundle)
    mrn = facts["patient"]["mrn"]
    patient_name = facts["patient"]["name"]
    summary_snippet = make_snippet(summary.summary_text) if summary else ""

    items = []
    latest_date = ""

    for entry in bundle["entry"]:
        resource = entry["resource"]
        resource_type = resource["resourceType"]

        if resource_type == "DocumentReference":
            attachment = resource["content"][0]["attachment"]
            text = base64.b64decode(attachment.get("data", "")).decode("utf-8")
            date = resource.get("context", {}).get("period", {}).get("start", "")
        elif resource_type == "DiagnosticReport":
            text = resource.get("conclusion", "")
            date = resource.get("effectiveDateTime", "")
        else:
            continue  # Patient and Encounter have no text worth searching

        if not text.strip():
            continue
        latest_date = max(latest_date, date)
        items.append(
            {
                "id": resource["id"],
                "text": text,
                "metadata": {
                    "mrn": mrn,
                    "patient_name": patient_name,
                    "resource_type": resource_type,
                    "resource_id": resource["id"],
                    "date": date,  # "" when unknown (Chroma does not allow None)
                    "date_int": date_to_int(date),
                    "snippet": make_snippet(text),
                    "summary_snippet": summary_snippet,
                },
            }
        )

    if summary is not None:
        # A summary has no clinical date of its own. We use the most recent
        # date in the record it summarizes, so date filters still make sense.
        items.append(
            {
                "id": f"summary-{mrn}",
                "text": summary.summary_text,
                "metadata": {
                    "mrn": mrn,
                    "patient_name": patient_name,
                    "resource_type": "Summary",
                    "resource_id": f"summary-{mrn}",
                    "date": latest_date,
                    "date_int": date_to_int(latest_date),
                    "snippet": make_snippet(summary.summary_text),
                    "summary_snippet": summary_snippet,
                },
            }
        )

    return items


def collect_items(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Search items for every stored bundle."""
    items = []
    for row in list_bundles(db_path):
        bundle = get_bundle(row["mrn"], db_path)
        summary = current_summary(row["mrn"], bundle, db_path)
        items.extend(bundle_items(bundle, summary))
    return items


def add_items(collection, items: list[dict], embed: Embedder) -> None:
    if not items:
        return
    texts = [item["text"] for item in items]
    collection.add(
        ids=[item["id"] for item in items],
        embeddings=embed(texts),  # one call for all texts: much faster than one by one
        documents=texts,
        metadatas=[item["metadata"] for item in items],
    )


def build_index(embed: Embedder, db_path: Path = DEFAULT_DB_PATH, chroma_path: Path = CHROMA_PATH) -> dict[str, int]:
    """Rebuild the collection from scratch. Returns item counts by type."""
    # From scratch, so deleted or changed records never linger in the index.
    collection = open_collection(chroma_path, reset=True)
    items = collect_items(db_path)
    add_items(collection, items, embed)

    counts: dict[str, int] = {}
    for item in items:
        resource_type = item["metadata"]["resource_type"]
        counts[resource_type] = counts.get(resource_type, 0) + 1
    return counts


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")  # for ANTHROPIC_MODEL (which summaries to use)
    counts = build_index(load_embedder())
    print("\n=== Task 4: search index ===\n")
    for resource_type, count in sorted(counts.items()):
        print(f"  {resource_type:<18} {count}")
    print(f"  {'Total':<18} {sum(counts.values())}")
    print(f"\nIndex: {CHROMA_PATH}\n")


if __name__ == "__main__":
    main()
