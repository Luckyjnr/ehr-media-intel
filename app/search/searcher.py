"""Run one semantic search against the ChromaDB collection."""

import datetime as dt

from app.search.index import Embedder, date_to_int


def build_filter(
    resource_type: str | None,
    date_from: dt.date | None,
    date_to: dt.date | None,
) -> dict | None:
    """Turn the optional filters into a Chroma "where" clause.

    Chroma wants a single condition on its own, but several wrapped in "$and".
    """
    conditions = []
    if resource_type:
        conditions.append({"resource_type": resource_type})
    if date_from:
        conditions.append({"date_int": {"$gte": date_to_int(date_from.isoformat())}})
    if date_to:
        conditions.append({"date_int": {"$lte": date_to_int(date_to.isoformat())}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def search(
    query: str,
    embed: Embedder,
    collection,
    resource_type: str | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    top_k: int = 5,
) -> list[dict]:
    """The top_k items closest in meaning to the query, best match first."""
    query_vector = embed([query])[0]
    response = collection.query(
        query_embeddings=[query_vector],
        n_results=top_k,
        where=build_filter(resource_type, date_from, date_to),
        include=["metadatas", "distances"],
    )

    results = []
    # Chroma answers for a list of queries; we sent one, so we read index [0].
    for metadata, distance in zip(response["metadatas"][0], response["distances"][0]):
        results.append(
            {
                # Cosine distance runs 0 (same meaning) to 2 (opposite), so
                # 1 - distance gives 1.0 for a perfect match. Higher is better.
                "score": round(1 - distance, 2),
                "mrn": metadata["mrn"],
                "patient_name": metadata["patient_name"],
                "resource_type": metadata["resource_type"],
                "resource_id": metadata["resource_id"],
                "date": metadata["date"] or None,
                "snippet": metadata["snippet"],
                "summary_snippet": metadata["summary_snippet"] or None,
            }
        )

    # Chroma already returns closest first; we sort anyway so the order is a
    # guarantee of this function, not a side effect of the library.
    results.sort(key=lambda result: result["score"], reverse=True)
    return results
