"""Unit tests for lead_qualifier.cleaning -- one behavior per test.

These target the exact messy formats observed in real lead exports (see
the CSV in the project root), plus the blank/garbage/None edge cases any
future export could also contain.
"""

from datetime import date

import pytest

from lead_qualifier import cleaning


# ---------------------------------------------------------------------------
# clean_text / clean_email / clean_website
# ---------------------------------------------------------------------------

class TestCleanText:
    def test_trims_whitespace(self):
        assert cleaning.clean_text("  Gbenga  ") == "Gbenga"

    def test_collapses_internal_whitespace(self):
        assert cleaning.clean_text("Head   of   Ops") == "Head of Ops"

    @pytest.mark.parametrize("raw", [None, "", "   ", "nan", "NaN", "N/A", float("nan")])
    def test_blank_variants_return_none(self, raw):
        assert cleaning.clean_text(raw) is None


class TestCleanEmail:
    def test_valid_email_lowercased(self):
        cleaned, valid = cleaning.clean_email("Gbenga@LuxAuto.io")
        assert cleaned == "gbenga@luxauto.io"
        assert valid is True

    @pytest.mark.parametrize("raw", ["ola[at]growthmedia.agency", "sara[at]upshiftmasons.io", "sophie(at)omniside.agency"])
    def test_deobfuscates_at(self, raw):
        cleaned, valid = cleaning.clean_email(raw)
        assert "@" in cleaned
        assert valid is True

    def test_no_domain_is_invalid(self):
        cleaned, valid = cleaning.clean_email("ivan@")
        assert valid is False

    def test_garbage_no_at_sign_is_invalid(self):
        cleaned, valid = cleaning.clean_email("weird-email-no-domain")
        assert cleaned is None
        assert valid is False

    def test_internal_space_is_invalid_but_preserved(self):
        # "deji m.@scaleforge" -- spaces get stripped but the result still
        # isn't a valid email (no TLD on the domain).
        cleaned, valid = cleaning.clean_email("deji m.@scaleforge")
        assert valid is False

    def test_blank_returns_none_and_invalid(self):
        assert cleaning.clean_email("") == (None, False)
        assert cleaning.clean_email(None) == (None, False)


class TestEmailDomain:
    def test_extracts_domain(self):
        assert cleaning.email_domain("gbenga@luxauto.io") == "luxauto.io"

    def test_none_input(self):
        assert cleaning.email_domain(None) is None

    def test_no_at_sign(self):
        assert cleaning.email_domain("notanemail") is None


class TestCleanWebsite:
    @pytest.mark.parametrize("raw,expected", [
        ("www.luxauto.io", "luxauto.io"),
        ("http://upshiftloop.agency", "upshiftloop.agency"),
        ("https://www.brightline.io/", "brightline.io"),
        ("luxauto.io", "luxauto.io"),
    ])
    def test_normalizes(self, raw, expected):
        assert cleaning.clean_website(raw) == expected

    def test_blank_returns_none(self):
        assert cleaning.clean_website("") is None
        assert cleaning.clean_website(None) is None


# ---------------------------------------------------------------------------
# parse_date
# ---------------------------------------------------------------------------

class TestParseDate:
    def test_mm_dd_yyyy_slash(self):
        assert cleaning.parse_date("06/28/2024") == date(2024, 6, 28)

    def test_iso_yyyy_mm_dd(self):
        assert cleaning.parse_date("2024-06-08") == date(2024, 6, 8)

    def test_iso_no_zero_pad(self):
        assert cleaning.parse_date("2024-6-19") == date(2024, 6, 19)

    def test_month_name(self):
        assert cleaning.parse_date("Jun 7 2024") == date(2024, 6, 7)

    def test_dd_mm_yyyy_hyphen_is_day_first(self):
        # 04-06-2024 is ambiguous but this dataset's hyphenated dates are
        # DD-MM-YYYY (confirmed by unambiguous rows like 23-06-2024).
        assert cleaning.parse_date("04-06-2024") == date(2024, 6, 4)

    def test_short_year_slash_is_month_first(self):
        assert cleaning.parse_date("6/1/24") == date(2024, 6, 1)

    def test_blank_returns_none(self):
        assert cleaning.parse_date("") is None
        assert cleaning.parse_date(None) is None

    def test_garbage_returns_none(self):
        assert cleaning.parse_date("asdf") is None


# ---------------------------------------------------------------------------
# parse_employees
# ---------------------------------------------------------------------------

class TestParseEmployees:
    def test_plain_integer(self):
        assert cleaning.parse_employees("20") == 20.0

    def test_range_midpoint(self):
        assert cleaning.parse_employees("35-55") == 45.0

    def test_approx_tilde(self):
        assert cleaning.parse_employees("~43") == 43.0

    def test_plus_suffix(self):
        assert cleaning.parse_employees("70+") == 70.0

    def test_combined_range_no_spaces(self):
        assert cleaning.parse_employees("28-48") == 38.0

    def test_garbage_returns_none(self):
        assert cleaning.parse_employees("asdf") is None

    def test_blank_returns_none(self):
        assert cleaning.parse_employees("") is None
        assert cleaning.parse_employees(None) is None


# ---------------------------------------------------------------------------
# parse_budget
# ---------------------------------------------------------------------------

class TestParseBudget:
    @pytest.mark.parametrize("raw,expected", [
        ("5,000/mo", 5000.0),
        ("$6k/mo", 6000.0),
        ("6k", 6000.0),
        ("$8,500", 8500.0),
        ("$9,000", 9000.0),
        ("10,000", 10000.0),
        ("15k/mo", 15000.0),
        ("0", 0.0),
        ("500", 500.0),
    ])
    def test_single_values(self, raw, expected):
        assert cleaning.parse_budget(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("$6-8k", 7000.0),
        ("5k-7k", 6000.0),
        ("8k-12k", 10000.0),
    ])
    def test_ranges_average(self, raw, expected):
        assert cleaning.parse_budget(raw) == expected

    @pytest.mark.parametrize("raw", ["TBD", "tbd", "depends", "", None])
    def test_unknown_values_return_none(self, raw):
        assert cleaning.parse_budget(raw) is None

    def test_explicit_zero_is_not_none(self):
        # "0" means "no budget", which is meaningfully different from
        # "TBD"/unknown -- must not collapse to None.
        assert cleaning.parse_budget("0") == 0.0


# ---------------------------------------------------------------------------
# parse_lead_id
# ---------------------------------------------------------------------------

class TestParseLeadId:
    def test_standard_id(self):
        info = cleaning.parse_lead_id("L-1369")
        assert info.canonical == "1369"
        assert info.looks_valid is True
        assert info.is_explicit_dup is False

    def test_missing_prefix_still_canonicalizes(self):
        info = cleaning.parse_lead_id("1341")
        assert info.canonical == "1341"
        assert info.looks_valid is True

    def test_explicit_dup_suffix(self):
        info = cleaning.parse_lead_id("L-1205-dup")
        assert info.canonical == "1205"
        assert info.is_explicit_dup is True

    def test_same_lead_different_formatting_share_canonical_id(self):
        a = cleaning.parse_lead_id("L-1373")
        b = cleaning.parse_lead_id("1373")
        assert a.canonical == b.canonical

    def test_garbage_id_is_invalid(self):
        info = cleaning.parse_lead_id("asdf")
        assert info.canonical is None
        assert info.looks_valid is False

    def test_blank_id_is_invalid(self):
        info = cleaning.parse_lead_id("")
        assert info.looks_valid is False


# ---------------------------------------------------------------------------
# Row-level junk detection
# ---------------------------------------------------------------------------

COLUMNS = [
    "lead_id", "created", "name", "email", "company",
    "employees", "website", "title", "source", "monthly_budget", "notes",
]


class TestIsBlankRow:
    def test_all_blank_is_true(self):
        row = {c: "" for c in COLUMNS}
        assert cleaning.is_blank_row(row, COLUMNS) is True

    def test_one_populated_field_is_false(self):
        row = {c: "" for c in COLUMNS}
        row["lead_id"] = "L-9001"
        assert cleaning.is_blank_row(row, COLUMNS) is False


class TestIsHeaderEchoRow:
    def test_literal_header_sentinel(self):
        row = {c: "" for c in COLUMNS}
        row["lead_id"] = "header"
        row["created"] = "lead_id"
        row["name"] = "name"
        assert cleaning.is_header_echo_row(row, COLUMNS) is True

    def test_normal_row_is_false(self):
        row = {c: "" for c in COLUMNS}
        row["lead_id"] = "L-1369"
        row["name"] = "Gbenga"
        row["company"] = "LuxAuto"
        assert cleaning.is_header_echo_row(row, COLUMNS) is False


class TestIsGarbageTestRow:
    def test_testrow_id(self):
        row = {c: "" for c in COLUMNS}
        row["lead_id"] = "TESTROW"
        assert cleaning.is_garbage_test_row(row) is True

    def test_qa_note_marker(self):
        row = {c: "" for c in COLUMNS}
        row["lead_id"] = "L-1"
        row["notes"] = "QA test entry, please ignore."
        assert cleaning.is_garbage_test_row(row) is True

    def test_repeated_nonsense_token(self):
        row = {c: "asdf" for c in COLUMNS}
        assert cleaning.is_garbage_test_row(row) is True

    def test_normal_row_is_false(self):
        row = {c: "" for c in COLUMNS}
        row["lead_id"] = "L-1369"
        row["name"] = "Gbenga"
        row["company"] = "LuxAuto"
        row["title"] = "Owner"
        row["source"] = "webform"
        row["notes"] = "We're a growth agency, budget approved."
        assert cleaning.is_garbage_test_row(row) is False
