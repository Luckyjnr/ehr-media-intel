"""Simple automatic checks on each summary.

These do not prove a summary is correct. They catch the most common
problems (too long, missing parts, a flagged lab left out, no disclaimer)
so a reviewer knows where to look first.
"""

from app.summaries.models import ClinicalSummary
from app.summaries.prompt import flagged_labs

MAX_WORDS = 200


def word_count(text: str) -> int:
    return len(text.split())


def check_all_parts_present(summary: ClinicalSummary, facts: dict) -> bool:
    """The four parts: chief concern, key diagnoses, recent media, flagged anomalies.

    A list may only be empty when the record has nothing to put in it
    (no documents -> no media, no flagged labs -> no anomalies).
    """
    if not summary.chief_concern.strip():
        return False
    if not summary.key_diagnoses:
        return False
    if facts["documents"] and not summary.recent_media:
        return False
    if flagged_labs(facts) and not summary.flagged_anomalies:
        return False
    return True


def check_flagged_labs_listed(summary: ClinicalSummary, facts: dict) -> bool:
    """Every high/low/critical lab's test name must appear in flagged_anomalies."""
    anomalies_text = " ".join(summary.flagged_anomalies).lower()
    for lab in flagged_labs(facts):
        if lab["name"].lower() not in anomalies_text:
            return False
    return True


def run_quality_checks(summary: ClinicalSummary, facts: dict) -> dict[str, str]:
    results = {
        "under_200_words": word_count(summary.summary_text) < MAX_WORDS,
        "all_parts_present": check_all_parts_present(summary, facts),
        "flagged_labs_listed": check_flagged_labs_listed(summary, facts),
        "disclaimer_present": bool(summary.disclaimer.strip()),
    }
    readable = {}
    for name, passed in results.items():
        readable[name] = "pass" if passed else "fail"
    return readable
