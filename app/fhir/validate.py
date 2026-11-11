"""Check bundles before we store them, and write the validation report.

Two checks:
1. Build every resource through fhir.resources (R4B). This catches missing
   required fields, wrong data types, badly formatted dates and unknown fields.
   Note: it does not check code values against their allowed lists (e.g. gender).
   Task 1 already limits those values, so that gap does not affect our data.
2. Every "reference" in the bundle must point to an entry inside the same bundle.
   A broken reference would make a document point at nobody.

Errors are returned as plain-English strings. Nothing here raises.
"""

import json
from pathlib import Path

from fhir.resources.R4B import get_fhir_model_class
from pydantic import ValidationError


def _describe_errors(label: str, error: ValidationError) -> list[str]:
    messages = []
    for detail in error.errors():
        field = ".".join(str(part) for part in detail["loc"]) or "(whole resource)"
        messages.append(f"{label}: field '{field}' is invalid: {detail['msg']}")
    return messages


def validate_resource(resource: dict) -> list[str]:
    """Build one resource through fhir.resources. Returns errors (empty = valid)."""
    resource_type = resource.get("resourceType", "Unknown")
    label = f"{resource_type} {resource.get('id', '(no id)')}"
    try:
        model_class = get_fhir_model_class(resource_type)
    except KeyError:
        return [f"{label}: '{resource_type}' is not a FHIR R4B resource type"]

    try:
        model_class.model_validate(resource)
    except ValidationError as error:
        return _describe_errors(label, error)
    return []


def _collect_references(value, found: list[str]) -> None:
    """Walk the nested JSON and collect every {"reference": "..."} value."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "reference" and isinstance(item, str):
                found.append(item)
            else:
                _collect_references(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_references(item, found)


def check_references(bundle: dict) -> list[str]:
    """Every reference must match the fullUrl of an entry in this bundle."""
    entries = bundle.get("entry", [])
    full_urls = set()
    for entry in entries:
        full_urls.add(entry.get("fullUrl"))

    errors = []
    for entry in entries:
        resource = entry.get("resource", {})
        references: list[str] = []
        _collect_references(resource, references)
        for reference in references:
            if reference not in full_urls:
                errors.append(
                    f"{resource.get('resourceType')} {resource.get('id')}: "
                    f"reference '{reference}' does not point to anything in the bundle"
                )
    return errors


def validate_bundle(bundle: dict) -> list[str]:
    """All errors for one bundle. An empty list means the bundle passed."""
    errors: list[str] = []
    for entry in bundle.get("entry", []):
        errors.extend(validate_resource(entry.get("resource", {})))

    # Only check the Bundle wrapper itself if the resources were fine,
    # otherwise the same resource error would be reported twice.
    if not errors:
        errors.extend(validate_resource(bundle))

    errors.extend(check_references(bundle))
    return errors


def write_report(report: dict, json_path: Path, markdown_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    summary = report["summary"]
    lines = [
        "# FHIR validation report",
        "",
        f"- Bundles built: {summary['bundles_built']}",
        f"- Passed: {summary['passed']}",
        f"- Failed: {summary['failed']}",
        f"- Held for review (MRN conflict): {summary['held_for_review']}",
        "",
        "## Errors",
        "",
    ]
    error_count = 0
    for bundle in report["bundles"]:
        for error in bundle["errors"]:
            lines.append(f"- MRN {bundle['mrn']}: {error}")
            error_count += 1
    if error_count == 0:
        lines.append("None.")

    lines.extend(["", "## Held for review", ""])
    if not report["held_for_review"]:
        lines.append("None.")
    for held in report["held_for_review"]:
        lines.append(f"- MRN {held['mrn']}: {held['reason']}")

    if report["unlinked_records"]:
        lines.extend(["", "## Records with no patient", ""])
        for record_id in report["unlinked_records"]:
            lines.append(f"- {record_id}")

    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
