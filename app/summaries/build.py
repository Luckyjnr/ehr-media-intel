"""Task 3 command line: stored bundles -> AI summaries -> cache -> report.

Run it with:
    python -m app.summaries.build

Set ANTHROPIC_API_KEY in .env to use Claude. Without a key (or with
SUMMARY_MODE=mock) it runs in mock mode and makes no API calls.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from app.fhir.store import DEFAULT_DB_PATH, get_bundle, list_bundles
from app.summaries.cache import get_cached_summary, record_hash, save_summary
from app.summaries.summarizer import (
    DEFAULT_MODEL,
    MOCK_MODEL,
    choose_mode,
    summarize_mock,
    summarize_with_claude,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = PROJECT_ROOT / "data" / "store" / "summaries_report.md"


def summarize_bundles(
    bundles: list[tuple[str, dict]],
    mode: str,
    model: str,
    client=None,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """Summarize each (mrn, bundle). Returns one result dict per bundle.

    status is "made", "cache_hit" or "failed". Failed summaries are not
    cached, so the next run tries them again.
    """
    # The model name is part of the cache key, so mock and real summaries never mix.
    model_for_hash = MOCK_MODEL if mode == "mock" else model
    results = []

    for mrn, bundle in bundles:
        hash_value = record_hash(bundle, model_for_hash)

        cached = get_cached_summary(mrn, hash_value, db_path)
        if cached is not None:
            results.append({"mrn": mrn, "status": "cache_hit", "summary": cached, "error": None, "api_calls": 0})
            continue

        if mode == "mock":
            outcome = summarize_mock(bundle)
        else:
            outcome = summarize_with_claude(bundle, client, model)

        if outcome.summary is not None:
            save_summary(outcome.summary, hash_value, db_path)
            status = "made"
        else:
            status = "failed"

        results.append(
            {
                "mrn": mrn,
                "status": status,
                "summary": outcome.summary,
                "error": outcome.error,
                "api_calls": outcome.api_calls,
            }
        )

    return results


def _count(results: list[dict], status: str) -> int:
    total = 0
    for result in results:
        if result["status"] == status:
            total += 1
    return total


def write_report(results: list[dict], mode: str, model: str, path: Path) -> None:
    api_calls = sum(result["api_calls"] for result in results)
    lines = [
        "# AI summaries report",
        "",
        f"- Mode: {mode} (model: {model if mode == 'api' else MOCK_MODEL})",
        f"- Bundles: {len(results)}",
        f"- Made: {_count(results, 'made')}",
        f"- Cache hits: {_count(results, 'cache_hit')}",
        f"- Failed: {_count(results, 'failed')}",
        f"- API calls this run: {api_calls}",
        "",
        "## Quality checks",
        "",
        "| MRN | Status | Confidence | Under 200 words | All parts | Flagged labs listed | Disclaimer |",
        "|---|---|---|---|---|---|---|",
    ]
    for result in results:
        summary = result["summary"]
        if summary is None:
            lines.append(f"| {result['mrn']} | failed | - | - | - | - | - |")
            continue
        checks = summary.quality_checks
        lines.append(
            f"| {result['mrn']} | {result['status']} | {summary.confidence} | "
            f"{checks.get('under_200_words')} | {checks.get('all_parts_present')} | "
            f"{checks.get('flagged_labs_listed')} | {checks.get('disclaimer_present')} |"
        )

    failures = [result for result in results if result["status"] == "failed"]
    if failures:
        lines.extend(["", "## Failures", ""])
        for result in failures:
            lines.append(f"- MRN {result['mrn']}: {result['error']}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    mode = choose_mode()
    model = os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL

    client = None
    if mode == "api":
        # Imported here so mock mode works even without the package configured.
        import anthropic

        # Reads ANTHROPIC_API_KEY from the environment. Keys that are not scoped to
        # a workspace must also send the workspace id, so we add it when it is set.
        headers = {}
        workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        if workspace_id:
            headers["anthropic-workspace-id"] = workspace_id
        client = anthropic.Anthropic(default_headers=headers)

    bundles = []
    for row in list_bundles():
        bundles.append((row["mrn"], get_bundle(row["mrn"])))

    results = summarize_bundles(bundles, mode, model, client)
    write_report(results, mode, model, REPORT_PATH)

    failed_quality = 0
    for result in results:
        summary = result["summary"]
        if summary is not None and "fail" in summary.quality_checks.values():
            failed_quality += 1

    print("\n=== Task 3: AI summaries ===\n")
    print(f"Mode:          {mode}" + (f" ({model})" if mode == "api" else ""))
    print(f"Bundles:       {len(results)}")
    print(f"Made:          {_count(results, 'made')}")
    print(f"Cache hits:    {_count(results, 'cache_hit')}")
    print(f"Failed:        {_count(results, 'failed')}")
    print(f"API calls:     {sum(result['api_calls'] for result in results)}")
    print(f"Quality:       {len(results) - _count(results, 'failed') - failed_quality} passed all checks, "
          f"{failed_quality} with at least one failed check")
    print(f"\nReport: {REPORT_PATH}\n")


if __name__ == "__main__":
    main()
