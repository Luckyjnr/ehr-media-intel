"""Small, pure cleaning functions.

"Pure" means: no file access, no global state, same input -> same output.
That makes each rule easy to unit test on its own.

Every function returns a pair: (cleaned_value, list_of_audit_entries).
The caller attaches the audit entries to the record being cleaned.
record_id and field are only used to label the audit entries.
"""

import datetime as dt
import re
from typing import Any

from app.ingestion.audit import AuditEntry, make_entry

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def is_blank(raw: Any) -> bool:
    """True for None, empty strings and strings that are only spaces."""
    return raw is None or str(raw).strip() == ""


def missing_field_entry(record_id: str, field: str) -> AuditEntry:
    return make_entry(
        record_id,
        field,
        None,
        None,
        "MISSING_FIELD",
        f"'{field}' was empty or missing in the source. Kept as empty.",
        "warning",
    )


def clean_text(raw: Any) -> str | None:
    """Trim a free-text value. Blank becomes None. Numbers become text."""
    if is_blank(raw):
        return None
    return str(raw).strip()


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

# Formats we accept, tried in order. We use a fixed list on purpose instead of a
# "guess any format" library: a wrong guess on a birth date is a patient-safety
# problem, so we only accept formats we have thought about.
KNOWN_DATE_FORMATS = [
    "%Y-%m-%d",  # 2024-03-05 (ISO, already canonical)
    "%Y%m%d",  # 20240305 (compact, common in HL7 feeds)
    "%d-%b-%Y",  # 05-Mar-2024
    "%d %b %Y",  # 05 Mar 2024
    "%B %d %Y",  # March 5 2024
    "%B %d, %Y",  # March 5, 2024
    "%b %d %Y",  # Mar 5 2024
    "%b %d, %Y",  # Mar 5, 2024
]

# Slash dates (03/05/2024, 5/3/24) need special handling because the order of
# month and day is unclear. See parse_slash_date below.
SLASH_DATE_PATTERN = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")

# "2024-03-05T14:20:00" -> we only need the calendar date for now.
ISO_DATETIME_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})T")


def expand_two_digit_year(two_digits: int, today: dt.date) -> int:
    """Turn '24' into 2024 and '65' into 1965.

    If 20xx would be in the future, it must mean 19xx.
    This is right for both visit dates and birth dates of living patients.
    """
    candidate = 2000 + two_digits
    if candidate > today.year:
        return 1900 + two_digits
    return candidate


def parse_slash_date(text: str, today: dt.date) -> tuple[dt.date | None, bool]:
    """Parse a slash date. Returns (date or None, was_it_ambiguous).

    Our main source is a US EHR (Epic), so we read slash dates as month/day.
    The exception: if the first number is over 12 it cannot be a month,
    so the date must be day/month (e.g. 21/08/1983).
    """
    match = SLASH_DATE_PATTERN.match(text)
    if match is None:
        return None, False

    first = int(match.group(1))
    second = int(match.group(2))
    year_text = match.group(3)
    if len(year_text) == 4:
        year = int(year_text)
    else:
        year = expand_two_digit_year(int(year_text), today)

    if first > 12:
        day = first
        month = second
        ambiguous = False
    else:
        month = first
        day = second
        # Ambiguous when both numbers could be a month, and swapping them
        # would give a different date. 04/04 reads the same either way.
        ambiguous = second <= 12 and first != second

    try:
        return dt.date(year, month, day), ambiguous
    except ValueError:
        # Not a real date, e.g. 02/30/2024.
        return None, False


def normalize_date(
    raw: Any,
    record_id: str = "",
    field: str = "date",
    today: dt.date | None = None,
) -> tuple[dt.date | None, list[AuditEntry]]:
    """Convert a date in any known format into a Python date (stored as ISO).

    Impossible dates and future dates are thrown away (None) with an error,
    because a wrong date is worse than no date in a medical record.
    `today` can be passed in so tests do not depend on the real clock.
    """
    if today is None:
        today = dt.date.today()

    if is_blank(raw):
        return None, [missing_field_entry(record_id, field)]

    entries: list[AuditEntry] = []
    original = str(raw).strip()
    text = original

    datetime_match = ISO_DATETIME_PATTERN.match(text)
    if datetime_match is not None:
        text = datetime_match.group(1)

    parsed: dt.date | None = None

    if SLASH_DATE_PATTERN.match(text):
        parsed, ambiguous = parse_slash_date(text, today)
        if parsed is not None and ambiguous:
            entries.append(
                make_entry(
                    record_id,
                    field,
                    original,
                    parsed.isoformat(),
                    "AMBIGUOUS_DATE",
                    f"'{original}' could be month/day or day/month. Read it as US "
                    f"month/day ({parsed.isoformat()}) because the source is a US EHR. "
                    "Please verify.",
                    "warning",
                )
            )
    else:
        for date_format in KNOWN_DATE_FORMATS:
            try:
                parsed = dt.datetime.strptime(text, date_format).date()
                break
            except ValueError:
                # Not this format, try the next one.
                continue

    if parsed is None:
        entries.append(
            make_entry(
                record_id,
                field,
                original,
                None,
                "DATE_INVALID",
                f"'{original}' is not a real calendar date in any known format. "
                "Removed so it cannot be used by mistake.",
                "error",
            )
        )
        return None, entries

    if parsed > today:
        entries.append(
            make_entry(
                record_id,
                field,
                original,
                None,
                "DATE_IN_FUTURE",
                f"'{original}' is in the future, which is not possible for "
                "clinical data. Removed.",
                "error",
            )
        )
        return None, entries

    if parsed.isoformat() != original:
        entries.append(
            make_entry(
                record_id,
                field,
                original,
                parsed.isoformat(),
                "DATE_NORMALIZED",
                "Converted to ISO format (YYYY-MM-DD).",
                "info",
            )
        )
    return parsed, entries


# ---------------------------------------------------------------------------
# Gender
# ---------------------------------------------------------------------------

# Every value maps to one of the four FHIR administrative-gender codes.
# Numeric codes follow ISO/IEC 5218, which many EHR exports use:
#   1 = male, 2 = female, 0 = not known, 9 = not applicable.
GENDER_MAP = {
    "m": "male",
    "male": "male",
    "1": "male",
    "f": "female",
    "female": "female",
    "2": "female",
    "o": "other",
    "other": "other",
    "u": "unknown",
    "unk": "unknown",
    "unknown": "unknown",
    "0": "unknown",
    "9": "unknown",
}


def normalize_gender(
    raw: Any, record_id: str = "", field: str = "gender"
) -> tuple[str, list[AuditEntry]]:
    if is_blank(raw):
        return "unknown", [missing_field_entry(record_id, field)]

    original = str(raw).strip()
    key = original.lower()

    if key not in GENDER_MAP:
        entry = make_entry(
            record_id,
            field,
            original,
            "unknown",
            "GENDER_UNRECOGNIZED",
            f"'{original}' is not a gender code we recognize. Stored as 'unknown'.",
            "warning",
        )
        return "unknown", [entry]

    cleaned = GENDER_MAP[key]
    entries: list[AuditEntry] = []
    if cleaned != original:
        entries.append(
            make_entry(
                record_id,
                field,
                original,
                cleaned,
                "GENDER_NORMALIZED",
                "Mapped to a FHIR administrative-gender code.",
                "info",
            )
        )
    return cleaned, entries


# ---------------------------------------------------------------------------
# MRN (Medical Record Number)
# ---------------------------------------------------------------------------

MRN_LENGTH = 7


def normalize_mrn(
    raw: Any, record_id: str = "", field: str = "mrn"
) -> tuple[str | None, list[AuditEntry]]:
    """Turn "MRN-0012345", " mrn 12345 ", "12-345" etc. into "0012345".

    If what is left is not purely digits, or is too long, we return None.
    We never guess an MRN: attaching data to the wrong patient is one of
    the most dangerous errors in healthcare IT.
    """
    if is_blank(raw):
        entry = make_entry(
            record_id,
            field,
            None,
            None,
            "MRN_MISSING",
            "No MRN in the source. We never guess an MRN, so the record is rejected.",
            "error",
        )
        return None, [entry]

    original = str(raw)
    text = original.strip().upper()

    if text.startswith("MRN"):
        text = text[3:]

    # Remove separators people type into MRNs.
    for separator in [" ", "-", ":", "#"]:
        text = text.replace(separator, "")

    # [0-9] rather than isdigit(), because isdigit() also accepts symbols like "²".
    is_all_digits = re.fullmatch(r"[0-9]+", text) is not None
    if not is_all_digits or len(text) > MRN_LENGTH:
        entry = make_entry(
            record_id,
            field,
            original,
            None,
            "MRN_INVALID",
            f"'{original}' is not a valid {MRN_LENGTH}-digit MRN. We never guess "
            "an MRN, so the record is rejected.",
            "error",
        )
        return None, [entry]

    cleaned = text.zfill(MRN_LENGTH)
    entries: list[AuditEntry] = []
    if cleaned != original:
        entries.append(
            make_entry(
                record_id,
                field,
                original,
                cleaned,
                "MRN_NORMALIZED",
                f"Removed prefix/separators/spaces and zero-padded to {MRN_LENGTH} digits.",
                "info",
            )
        )
    return cleaned, entries


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def normalize_name(
    raw: Any, record_id: str = "", field: str = "name"
) -> tuple[str | None, list[AuditEntry]]:
    """Trim, collapse repeated spaces, and Title Case.

    Known limitation: "mcdonald" becomes "Mcdonald", not "McDonald".
    That is acceptable for matching, and the original stays in the audit log.
    """
    if is_blank(raw):
        return None, [missing_field_entry(record_id, field)]

    original = str(raw)
    # split() with no arguments splits on any run of whitespace and drops
    # spaces at both ends, so joining with one space collapses them.
    collapsed = " ".join(original.split())
    cleaned = collapsed.title()

    entries: list[AuditEntry] = []
    if cleaned != original:
        entries.append(
            make_entry(
                record_id,
                field,
                original,
                cleaned,
                "NAME_NORMALIZED",
                "Trimmed spaces and changed to Title Case.",
                "info",
            )
        )
    return cleaned, entries


def split_full_name(raw: Any) -> tuple[str | None, str | None]:
    """Split one name string into (given, family).

    "SMITH, JOHN" -> ("JOHN", "SMITH")   (comma means "Last, First")
    "maria garcia" -> ("maria", "garcia") (otherwise the last word is the family name)
    Casing is fixed later by normalize_name.
    """
    if is_blank(raw):
        return None, None

    text = " ".join(str(raw).split())

    if "," in text:
        family, given = text.split(",", 1)
        given = given.strip() or None
        family = family.strip() or None
        return given, family

    words = text.split(" ")
    if len(words) == 1:
        return None, words[0]
    given = " ".join(words[:-1])
    family = words[-1]
    return given, family


# ---------------------------------------------------------------------------
# Document types and lab flags
# ---------------------------------------------------------------------------

DOC_TYPE_MAP = {
    "discharge summary": "discharge_summary",
    "discharge_summary": "discharge_summary",
    "ds": "discharge_summary",
    "imaging": "imaging",
    "radiology": "imaging",
    "x-ray": "imaging",
    "ct": "imaging",
    "mri": "imaging",
    "lab report": "lab_report",
    "lab_report": "lab_report",
    "lab": "lab_report",
    "clinical note": "clinical_note",
    "clinical_note": "clinical_note",
    "progress note": "clinical_note",
    "clinic note": "clinical_note",
    "consult": "clinical_note",
    "consult note": "clinical_note",
    "other": "other",
}


def normalize_doc_type(
    raw: Any, record_id: str = "", field: str = "doc_type"
) -> tuple[str, list[AuditEntry]]:
    if is_blank(raw):
        return "other", [missing_field_entry(record_id, field)]

    original = str(raw).strip()
    key = " ".join(original.lower().split())

    if key not in DOC_TYPE_MAP:
        entry = make_entry(
            record_id,
            field,
            original,
            "other",
            "DOC_TYPE_UNRECOGNIZED",
            f"'{original}' is not a document type we know. Stored as 'other'.",
            "warning",
        )
        return "other", [entry]

    cleaned = DOC_TYPE_MAP[key]
    entries: list[AuditEntry] = []
    if cleaned != original:
        entries.append(
            make_entry(
                record_id, field, original, cleaned, "DOC_TYPE_NORMALIZED",
                "Mapped to a standard document type.", "info",
            )
        )
    return cleaned, entries


# HH/LL ("critically high/low") are treated as critical: they need urgent action.
FLAG_MAP = {
    "n": "normal",
    "normal": "normal",
    "h": "high",
    "high": "high",
    "l": "low",
    "low": "low",
    "hh": "critical",
    "ll": "critical",
    "c": "critical",
    "critical": "critical",
    "panic": "critical",
}


def normalize_flag(
    raw: Any, record_id: str = "", field: str = "flag"
) -> tuple[str | None, list[AuditEntry]]:
    if is_blank(raw):
        return None, [missing_field_entry(record_id, field)]

    original = str(raw).strip()
    key = original.lower()

    if key not in FLAG_MAP:
        entry = make_entry(
            record_id, field, original, None, "FLAG_UNRECOGNIZED",
            f"'{original}' is not a lab flag we know. Left empty.", "warning",
        )
        return None, [entry]

    cleaned = FLAG_MAP[key]
    entries: list[AuditEntry] = []
    if cleaned != original:
        entries.append(
            make_entry(
                record_id, field, original, cleaned, "FLAG_NORMALIZED",
                "Mapped to normal/high/low/critical.", "info",
            )
        )
    return cleaned, entries
