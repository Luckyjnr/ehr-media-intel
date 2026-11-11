"""Turn a FHIR bundle into short readable text, and hold the prompt rules.

Bump PROMPT_VERSION whenever SYSTEM_PROMPT changes. It is part of the cache
key, so changing it makes every summary regenerate with the new rules.
"""

import base64
import re

PROMPT_VERSION = "v1"

NOT_DOCUMENTED = "Not documented"

# Task 2 writes each lab's flag into the DiagnosticReport conclusion in a fixed
# format: "Glucose: 182 mg/dL (high)." We read the flag back from there.
FLAG_PATTERN = re.compile(r"\((normal|high|low|critical)\)\.")
ABNORMAL_FLAGS = {"high", "low", "critical"}

SYSTEM_PROMPT = """You write short clinical summaries of one patient's record for clinicians.

Rules:
1. Use ONLY facts written in the record you are given. Never invent diagnoses, dates, values, medications or history.
2. If something is not in the record, write "Not documented". Do not guess.
3. List EVERY lab marked FLAG: high, low or critical in flagged_anomalies. Start each item with the exact test name as written in the record, then the value, unit and flag.
4. recent_media has one item per document in the record: its type, its date, and one short note on what it shows.
5. key_diagnoses lists diagnoses that the documents state. If none are stated, use ["Not documented"].
6. summary_text must be under 200 words, in plain clinical English.
7. confidence: "high" if the record is complete and consistent, "medium" if some facts are missing, "low" if the record is sparse or unclear. Explain why in confidence_reason (one sentence).
8. Reply with JSON only. No markdown, no code fences, no text before or after. Use exactly this shape:

{
  "chief_concern": "string",
  "key_diagnoses": ["string"],
  "recent_media": [{"type": "string", "date": "YYYY-MM-DD or Not documented", "note": "string"}],
  "flagged_anomalies": ["string"],
  "summary_text": "string",
  "confidence": "low | medium | high",
  "confidence_reason": "string"
}"""


def extract_facts(bundle: dict) -> dict:
    """Pull the facts we need out of a bundle into a simple dict.

    Used by the prompt, by mock mode and by the quality checks, so all
    three see exactly the same facts.
    """
    patient = {"mrn": NOT_DOCUMENTED, "name": NOT_DOCUMENTED, "gender": NOT_DOCUMENTED, "birth_date": NOT_DOCUMENTED}
    documents = []
    labs = []

    for entry in bundle.get("entry", []):
        resource = entry["resource"]
        resource_type = resource["resourceType"]

        if resource_type == "Patient":
            identifiers = resource.get("identifier", [])
            if identifiers:
                patient["mrn"] = identifiers[0].get("value", NOT_DOCUMENTED)
            names = resource.get("name", [])
            if names:
                name_parts = names[0].get("given", []) + [names[0].get("family", "")]
                full_name = " ".join(part for part in name_parts if part)
                patient["name"] = full_name or NOT_DOCUMENTED
            patient["gender"] = resource.get("gender", NOT_DOCUMENTED)
            patient["birth_date"] = resource.get("birthDate", NOT_DOCUMENTED)

        elif resource_type == "DocumentReference":
            attachment = resource["content"][0]["attachment"]
            # The text was stored base64-encoded in Task 2, so decode it back.
            text = base64.b64decode(attachment.get("data", "")).decode("utf-8")
            period = resource.get("context", {}).get("period", {})
            documents.append(
                {
                    "type": resource.get("type", {}).get("text", "Document"),
                    "date": period.get("start", NOT_DOCUMENTED),
                    "title": resource.get("description") or attachment.get("title") or "Untitled",
                    "text": text,
                }
            )

        elif resource_type == "DiagnosticReport":
            conclusion = resource.get("conclusion", "")
            match = FLAG_PATTERN.search(conclusion)
            labs.append(
                {
                    "name": resource.get("code", {}).get("text", "Unknown test"),
                    "date": resource.get("effectiveDateTime", NOT_DOCUMENTED),
                    "result": conclusion,
                    "flag": match.group(1) if match else None,
                }
            )

    return {"patient": patient, "documents": documents, "labs": labs}


def flagged_labs(facts: dict) -> list[dict]:
    """Labs marked high, low or critical."""
    result = []
    for lab in facts["labs"]:
        if lab["flag"] in ABNORMAL_FLAGS:
            result.append(lab)
    return result


def bundle_to_text(facts: dict) -> str:
    """Readable plain text for the model. Shorter and clearer than raw FHIR JSON."""
    patient = facts["patient"]
    lines = [
        "PATIENT",
        f"Name: {patient['name']}",
        f"MRN: {patient['mrn']}",
        f"Gender: {patient['gender']}",
        f"Birth date: {patient['birth_date']}",
        "",
        f"DOCUMENTS ({len(facts['documents'])})",
    ]
    if not facts["documents"]:
        lines.append("None.")
    for number, doc in enumerate(facts["documents"], start=1):
        lines.append(f"[{number}] {doc['type']} | {doc['date']} | {doc['title']}")
        lines.append(doc["text"])
        lines.append("")

    lines.append(f"LABS ({len(facts['labs'])})")
    if not facts["labs"]:
        lines.append("None.")
    for lab in facts["labs"]:
        flag = lab["flag"] or "none"
        lines.append(f"- {lab['date']} | {lab['result']} | FLAG: {flag}")

    return "\n".join(lines)


def build_user_message(facts: dict) -> str:
    return (
        "Summarize this patient record. Follow the rules exactly.\n\n"
        "<record>\n"
        f"{bundle_to_text(facts)}\n"
        "</record>"
    )
