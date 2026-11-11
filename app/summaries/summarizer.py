"""Make one ClinicalSummary from one FHIR bundle, with Claude or in mock mode.

Never raises for a bad reply: it returns a SummaryOutcome with an error instead,
so one bad patient cannot stop the whole run.
"""

import datetime as dt
import json
import os

import anthropic
from pydantic import BaseModel, ValidationError

from app.summaries.models import ClinicalSummary, MediaItem, ModelDraft
from app.summaries.prompt import (
    NOT_DOCUMENTED,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_message,
    extract_facts,
    flagged_labs,
)
from app.summaries.quality import MAX_WORDS, run_quality_checks, word_count

DEFAULT_MODEL = "claude-haiku-5-5"
MOCK_MODEL = "mock"

# Set by our code on every summary. The model is never asked to write it.
DISCLAIMER = (
    "AI-generated summary for information only. Not a clinical decision. "
    "Verify against the source record."
)

# Room for the model's internal reasoning plus the JSON reply.
MAX_TOKENS = 16000


class SummaryOutcome(BaseModel):
    summary: ClinicalSummary | None = None
    error: str | None = None
    api_calls: int = 0


def choose_mode() -> str:
    """'mock' if SUMMARY_MODE=mock or there is no API key, otherwise 'api'."""
    if os.environ.get("SUMMARY_MODE", "").lower() == "mock":
        return "mock"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "mock"
    return "api"


def _finish(draft: ModelDraft, facts: dict, model: str) -> ClinicalSummary:
    """Add the fields our code owns, then run the quality checks."""
    summary = ClinicalSummary(
        **draft.model_dump(),
        mrn=facts["patient"]["mrn"],
        patient_name=facts["patient"]["name"],
        disclaimer=DISCLAIMER,
        model=model,
        prompt_version=PROMPT_VERSION,
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )
    summary.quality_checks = run_quality_checks(summary, facts)
    return summary


# ---------------------------------------------------------------------------
# Mock mode: no API call, built straight from the data
# ---------------------------------------------------------------------------


def build_mock_draft(facts: dict) -> ModelDraft:
    """A plain summary from the facts, so the project runs without an API key.

    It does not interpret anything, so it never claims diagnoses.
    """
    documents = facts["documents"]
    media = []
    for doc in documents:
        media.append(MediaItem(type=doc["type"], date=doc["date"], note=doc["title"]))

    anomalies = []
    for lab in flagged_labs(facts):
        anomalies.append(lab["result"])

    if documents:
        chief_concern = documents[0]["title"]
    else:
        chief_concern = NOT_DOCUMENTED

    patient = facts["patient"]
    summary_text = (
        f"{patient['name']} ({patient['gender']}, born {patient['birth_date']}). "
        f"Record has {len(documents)} document(s) and {len(facts['labs'])} lab result(s), "
        f"with {len(anomalies)} flagged as abnormal. "
        "Mock mode: generated from the record without AI."
    )

    return ModelDraft(
        chief_concern=chief_concern,
        key_diagnoses=[NOT_DOCUMENTED],
        recent_media=media,
        flagged_anomalies=anomalies,
        summary_text=summary_text,
        confidence="low",
        confidence_reason="Mock mode: no AI review of the documents.",
    )


def summarize_mock(bundle: dict) -> SummaryOutcome:
    facts = extract_facts(bundle)
    return SummaryOutcome(summary=_finish(build_mock_draft(facts), facts, MOCK_MODEL))


# ---------------------------------------------------------------------------
# API mode
# ---------------------------------------------------------------------------


def _reply_text(response) -> str:
    # The reply can contain several blocks (e.g. thinking). We want the text ones.
    parts = []
    for block in response.content:
        if block.type == "text":
            parts.append(block.text)
    return "".join(parts).strip()


def _strip_code_fence(text: str) -> str:
    # The prompt says "no code fences", but tolerate ```json ... ``` anyway.
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def parse_reply(text: str) -> tuple[ModelDraft | None, str | None]:
    """Returns (draft, None) if the reply is usable, else (None, reason)."""
    try:
        data = json.loads(_strip_code_fence(text))
    except json.JSONDecodeError as error:
        return None, f"reply was not valid JSON ({error.msg})"

    try:
        draft = ModelDraft.model_validate(data)
    except ValidationError as error:
        first = error.errors()[0]
        field = ".".join(str(part) for part in first["loc"])
        return None, f"reply JSON had the wrong shape: '{field}' {first['msg']}"

    words = word_count(draft.summary_text)
    if words >= MAX_WORDS:
        return None, f"summary_text has {words} words; it must be under {MAX_WORDS}"

    return draft, None


def summarize_with_claude(bundle: dict, client, model: str) -> SummaryOutcome:
    """Call Claude, check the reply, and retry once with feedback if it is unusable."""
    facts = extract_facts(bundle)
    messages = [{"role": "user", "content": build_user_message(facts)}]
    api_calls = 0
    errors = []

    for attempt in [1, 2]:  # first try, then one retry
        try:
            # No temperature setting: this model only accepts the default, and
            # the current API version rejects the parameter for every model.
            # Consistency comes from the strict prompt, the checks below, and
            # the cache (an unchanged record always returns the same summary).
            response = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=messages,
            )
        except anthropic.APIError as error:
            # The SDK already retried rate limits and server errors. Record and move on.
            api_calls += 1
            errors.append(f"attempt {attempt}: API error: {error}")
            break
        api_calls += 1

        if response.stop_reason in ("refusal", "max_tokens"):
            errors.append(f"attempt {attempt}: reply stopped early ({response.stop_reason})")
            continue

        reply = _reply_text(response)
        draft, error = parse_reply(reply)
        if draft is not None:
            return SummaryOutcome(summary=_finish(draft, facts, model), api_calls=api_calls)

        errors.append(f"attempt {attempt}: {error}")
        # Show the model its reply and what was wrong, so the retry can fix it.
        messages = messages + [
            {"role": "assistant", "content": reply or "(empty reply)"},
            {
                "role": "user",
                "content": f"That reply could not be used: {error}. "
                "Reply again with JSON only, in exactly the requested shape.",
            },
        ]

    return SummaryOutcome(error="; ".join(errors), api_calls=api_calls)
