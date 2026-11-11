"""Unit tests for the small cleaning functions. No files are read."""

import datetime as dt

import pytest

from app.ingestion.cleaners import (
    normalize_date,
    normalize_gender,
    normalize_mrn,
    normalize_name,
    split_full_name,
)

# A fixed "today" so tests give the same result every day.
TODAY = dt.date(2026, 1, 1)


def rules(entries):
    """List of rule codes, to make assertions short and readable."""
    return [entry.rule for entry in entries]


# --- Dates -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "2024-03-05",
        "2024-03-05T14:20:00",
        "03/05/2024",
        "3/5/24",
        "05-Mar-2024",
        "March 5 2024",
        "March 5, 2024",
        "20240305",
    ],
)
def test_many_date_formats_become_the_same_iso_date(raw):
    value, _ = normalize_date(raw, today=TODAY)
    assert value == dt.date(2024, 3, 5)
    assert value.isoformat() == "2024-03-05"


def test_iso_date_needs_no_audit_entry_but_other_formats_are_logged():
    _, iso_entries = normalize_date("2024-03-05", today=TODAY)
    _, other_entries = normalize_date("20240305", today=TODAY)
    assert iso_entries == []
    assert rules(other_entries) == ["DATE_NORMALIZED"]


def test_ambiguous_slash_date_is_read_as_us_and_logs_a_warning():
    value, entries = normalize_date("5/3/24", today=TODAY)
    assert value == dt.date(2024, 5, 3)  # US month/day: May 3
    warning = [e for e in entries if e.rule == "AMBIGUOUS_DATE"]
    assert len(warning) == 1
    assert warning[0].severity == "warning"


def test_slash_date_with_first_number_over_12_is_day_month_and_not_ambiguous():
    value, entries = normalize_date("21/08/1983", today=TODAY)
    assert value == dt.date(1983, 8, 21)
    assert "AMBIGUOUS_DATE" not in rules(entries)


def test_slash_date_with_second_number_over_12_is_not_ambiguous():
    value, entries = normalize_date("11/30/1972", today=TODAY)
    assert value == dt.date(1972, 11, 30)
    assert "AMBIGUOUS_DATE" not in rules(entries)


def test_two_digit_year_in_the_past_century():
    value, _ = normalize_date("5/3/65", today=TODAY)
    assert value == dt.date(1965, 5, 3)


@pytest.mark.parametrize("raw", ["02/30/2024", "2024-13-01", "not a date"])
def test_impossible_date_becomes_none_with_an_error(raw):
    value, entries = normalize_date(raw, today=TODAY)
    assert value is None
    assert rules(entries) == ["DATE_INVALID"]
    assert entries[0].severity == "error"
    assert entries[0].old_value == raw  # original kept in the audit log


def test_future_date_becomes_none_with_an_error():
    value, entries = normalize_date("2027-01-15", today=TODAY)
    assert value is None
    assert rules(entries) == ["DATE_IN_FUTURE"]
    assert entries[0].severity == "error"


def test_missing_date_is_none_with_a_missing_field_warning():
    value, entries = normalize_date("  ", field="birth_date", today=TODAY)
    assert value is None
    assert rules(entries) == ["MISSING_FIELD"]
    assert entries[0].field == "birth_date"


# --- Gender ----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("M", "male"),
        ("m", "male"),
        ("Male", "male"),
        ("1", "male"),
        (1, "male"),
        ("F", "female"),
        ("female", "female"),
        ("2", "female"),
        ("U", "unknown"),
        ("other", "other"),
    ],
)
def test_gender_variants_map_to_fhir_codes(raw, expected):
    value, _ = normalize_gender(raw)
    assert value == expected


def test_blank_gender_is_unknown_with_missing_field_warning():
    value, entries = normalize_gender("")
    assert value == "unknown"
    assert rules(entries) == ["MISSING_FIELD"]


def test_unrecognized_gender_is_unknown_with_a_warning():
    value, entries = normalize_gender("X7")
    assert value == "unknown"
    assert rules(entries) == ["GENDER_UNRECOGNIZED"]
    assert entries[0].severity == "warning"


# --- MRN -------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["12345", "MRN-0012345", " mrn 12345 ", "0012345", "12-345", "MRN: 12345"])
def test_mrn_variants_all_become_the_same_7_digit_value(raw):
    value, _ = normalize_mrn(raw)
    assert value == "0012345"


@pytest.mark.parametrize("raw", [None, "", "   ", "MRN-", "ABC123", "12345678"])
def test_unusable_mrn_is_never_guessed(raw):
    value, entries = normalize_mrn(raw)
    assert value is None
    assert entries[0].severity == "error"
    assert entries[0].rule in ("MRN_MISSING", "MRN_INVALID")


# --- Names -----------------------------------------------------------------


def test_names_are_trimmed_collapsed_and_title_cased():
    value, entries = normalize_name("  mARIA   garcia ")
    assert value == "Maria Garcia"
    assert rules(entries) == ["NAME_NORMALIZED"]


def test_split_full_name_handles_last_comma_first_and_first_last():
    assert split_full_name("SMITH, JOHN") == ("JOHN", "SMITH")
    assert split_full_name("maria   garcia") == ("maria", "garcia")
    assert split_full_name(None) == (None, None)
