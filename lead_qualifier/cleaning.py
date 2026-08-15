"""Field-level cleaning and parsing utilities.

Every function here is a small, pure, independently-testable unit that takes
one messy raw value (as it might appear in a CSV cell -- a string, ``None``,
``NaN``, or occasionally a number pandas has already coerced) and returns a
normalized value plus, where useful, a validity flag.

None of these functions raise on bad input -- malformed data is extremely
common in a real lead export, so "I don't know" (``None`` / ``False``) is
always a valid, expected answer.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from dateutil import parser as _dateutil_parser

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

_BLANK_TOKENS = {"", "nan", "none", "null", "n/a", "na", "-", "?"}


def _to_str(raw: Any) -> Optional[str]:
    """Coerce a raw cell value to a trimmed string, or None if it's blank.

    Handles ``None``, float ``NaN`` (as pandas produces for empty CSV cells)
    and plain whitespace uniformly.
    """
    if raw is None:
        return None
    if isinstance(raw, float) and math.isnan(raw):
        return None
    s = str(raw).strip()
    if not s or s.lower() in _BLANK_TOKENS:
        return None
    return s


def clean_text(raw: Any) -> Optional[str]:
    """Trim a free-text field (name, title, company, source, notes).

    Collapses internal whitespace runs but otherwise leaves casing/content
    alone -- we don't want to mangle names or notes.
    """
    s = _to_str(raw)
    if s is None:
        return None
    return re.sub(r"\s+", " ", s).strip()


def clean_company(raw: Any) -> Optional[str]:
    """Clean a company-name cell. Thin wrapper around clean_text() so callers
    have one named function per column, even though the logic is identical."""
    return clean_text(raw)


def clean_name(raw: Any) -> Optional[str]:
    """Clean a contact-name cell (see clean_company -- same logic, own name)."""
    return clean_text(raw)


def clean_title(raw: Any) -> Optional[str]:
    """Clean a job-title cell (see clean_company -- same logic, own name)."""
    return clean_text(raw)


def clean_source(raw: Any) -> Optional[str]:
    """Clean a lead-source cell (webform/linkedin/referral/...) and
    lower-case it so downstream comparisons/grouping are case-insensitive."""
    s = clean_text(raw)
    return s.lower() if s else None


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_OBFUSCATED_AT_RE = re.compile(r"\s*[\[(]\s*at\s*[\])]\s*", flags=re.IGNORECASE)


def clean_email(raw: Any) -> tuple[Optional[str], bool]:
    """Normalize an email address and report whether it looks valid.

    Returns ``(cleaned_email_or_None, is_valid)``. Handles common
    de-obfuscation (``name[at]domain.com``) and strips stray whitespace.
    An email that can't be salvaged is still returned lower-cased/trimmed
    (so a human can review it) but flagged invalid.
    """
    s = _to_str(raw)
    if s is None:
        return None, False
    s = _OBFUSCATED_AT_RE.sub("@", s)
    s = s.replace(" ", "")
    s = s.lower()
    if not s or "@" not in s:
        return None, False
    is_valid = bool(_EMAIL_RE.match(s))
    return s, is_valid


def email_domain(cleaned_email: Optional[str]) -> Optional[str]:
    """Return the domain half of an already-cleaned email, or None.

    Expects output from clean_email() -- doesn't re-validate or re-normalize.
    Used, e.g., to compare a lead's email domain against their website.
    """
    if not cleaned_email or "@" not in cleaned_email:
        return None
    # rsplit from the right so a (rare, invalid) address with multiple "@"
    # still yields the last/domain-looking segment rather than crashing.
    return cleaned_email.rsplit("@", 1)[-1] or None


# ---------------------------------------------------------------------------
# Website
# ---------------------------------------------------------------------------

_SCHEME_RE = re.compile(r"^https?://", flags=re.IGNORECASE)
_WWW_RE = re.compile(r"^www\.", flags=re.IGNORECASE)


def clean_website(raw: Any) -> Optional[str]:
    """Normalize a website to a bare lower-case domain (no scheme/www/slash)."""
    s = _to_str(raw)
    if s is None:
        return None
    s = _SCHEME_RE.sub("", s)
    s = _WWW_RE.sub("", s)
    s = s.rstrip("/")
    s = s.lower().strip()
    return s or None


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")


def parse_date(raw: Any) -> Optional[date]:
    """Parse a "created" date from any of the formats seen in lead exports.

    Observed formats include ``MM/DD/YYYY``, ``M/D/YY``, ``YYYY-MM-DD``,
    ``DD-MM-YYYY`` and ``Mon D YYYY``. Slash- and hyphen-separated dates are
    ambiguous on their own, so we apply one heuristic consistently:

    * ISO-style (``YYYY-M-D``) is unambiguous and parsed as-is.
    * Hyphenated non-ISO dates (``DD-MM-YYYY``) are treated **day-first**.
    * Slash-separated dates (``MM/DD/YYYY`` or ``M/D/YY``) are treated
      **month-first** (US convention).

    Returns ``None`` for anything that can't be parsed -- never raises.
    """
    s = _to_str(raw)
    if s is None:
        return None
    is_iso = bool(_ISO_DATE_RE.match(s))
    dayfirst = ("-" in s) and not is_iso
    try:
        dt = _dateutil_parser.parse(s, dayfirst=dayfirst)
    except (ValueError, OverflowError, TypeError):
        return None
    return dt.date()


# ---------------------------------------------------------------------------
# Employees (headcount)
# ---------------------------------------------------------------------------

_EMP_RANGE_RE = re.compile(r"(\d+)\s*-\s*(\d+)")
_EMP_APPROX_RE = re.compile(r"~\s*(\d+)")
_EMP_PLUS_RE = re.compile(r"(\d+)\s*\+")
_EMP_DIGITS_RE = re.compile(r"\d+")


def parse_employees(raw: Any) -> Optional[float]:
    """Estimate headcount from messy free-form employee-count text.

    ``"35-55"`` -> midpoint 45. ``"~43"`` / ``"70+"`` -> 43 / 70 (best
    single-number estimate). Plain digits parse directly. Anything with no
    recognizable number (``"asdf"``) returns ``None``.
    """
    s = _to_str(raw)
    if s is None:
        return None

    # Order matters: check the most specific pattern first so e.g. "~43"
    # doesn't fall through to the generic "any digits" catch-all before the
    # "~" is noticed.

    # "35-55" -> midpoint of the range.
    m = _EMP_RANGE_RE.search(s)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return (lo + hi) / 2

    # "~43" -> take the approximate number as-is.
    m = _EMP_APPROX_RE.search(s)
    if m:
        return float(m.group(1))

    # "70+" -> take the lower-bound number as-is.
    m = _EMP_PLUS_RE.search(s)
    if m:
        return float(m.group(1))

    # Plain "20" with nothing else going on.
    if s.isdigit():
        return float(s)

    # Last resort: grab the first run of digits from whatever's left
    # (covers odd formatting we haven't seen but that still has a number).
    m = _EMP_DIGITS_RE.search(s)
    if m:
        return float(m.group(0))

    # No number anywhere in the string (e.g. "asdf") -> genuinely unknown.
    return None


# ---------------------------------------------------------------------------
# Monthly budget
# ---------------------------------------------------------------------------

_BUDGET_SKIP_TOKENS = {"tbd", "depends", "unknown", "?", ""}
_BUDGET_RANGE_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(k)?\s*-\s*(\d+(?:\.\d+)?)\s*(k)?$"
)
_BUDGET_SINGLE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(k)?$")


def parse_budget(raw: Any) -> Optional[float]:
    """Estimate a monthly USD budget figure from free-form budget text.

    Handles ``$``, thousands separators, ``k`` shorthand, ``/mo`` suffixes
    and ranges (``"$6-8k"``, ``"5k-7k"`` -> midpoint). Non-numeric answers
    (``"TBD"``, ``"depends"``) return ``None`` -- meaning "unknown", not
    zero. An explicit ``"0"`` is preserved as ``0.0``.
    """
    s = _to_str(raw)
    if s is None:
        return None

    # Strip everything that's just decoration, not part of the number:
    # currency sign, thousands separators, and "/mo"|"per month" suffixes.
    s = s.lower().replace("$", "").replace(",", "")
    s = re.sub(r"/\s*mo(nth)?\b", "", s)
    s = re.sub(r"\bper\s*month\b", "", s)
    s = s.strip()

    # Words that explicitly mean "no numeric answer given" -- distinct from
    # an explicit "0", which is a real (if unwelcome) budget figure.
    if s in _BUDGET_SKIP_TOKENS:
        return None

    # Range, e.g. "6-8k" or "5k-7k" -> average the two ends. The "k" suffix
    # may appear on either/both numbers ("$6-8k" means 6k-8k, not 6-8k), so
    # if it shows up anywhere in the pair we apply it to both.
    m = _BUDGET_RANGE_RE.match(s)
    if m:
        lo, k_lo, hi, k_hi = m.groups()
        has_k = bool(k_lo or k_hi)
        mult = 1000.0 if has_k else 1.0
        return (float(lo) * mult + float(hi) * mult) / 2

    # Single value, e.g. "6k", "8500", "0".
    m = _BUDGET_SINGLE_RE.match(s)
    if m:
        val, k = m.groups()
        result = float(val)
        if k:
            result *= 1000.0
        return result

    # Didn't match any known shape -- treat as unknown rather than guessing.
    return None


# ---------------------------------------------------------------------------
# Lead ID
# ---------------------------------------------------------------------------

_LEAD_ID_DIGITS_RE = re.compile(r"(\d{2,})")
_DUP_SUFFIX_RE = re.compile(r"-dup$", flags=re.IGNORECASE)


@dataclass(frozen=True)
class LeadIdInfo:
    raw: Optional[str]
    canonical: Optional[str]  # digits-only id, e.g. "1369"; None if unparseable
    is_explicit_dup: bool  # id carried an explicit "-dup" suffix
    looks_valid: bool  # a plausible numeric id was found


def parse_lead_id(raw: Any) -> LeadIdInfo:
    """Extract a canonical, dedup-friendly id from a lead_id cell.

    Both ``"L-1369"`` and the inconsistently-formatted ``"1369"`` normalize
    to canonical id ``"1369"``. IDs with no recognizable number (``"asdf"``,
    ``"TESTROW"``) are flagged ``looks_valid=False``.
    """
    s = _to_str(raw)
    if s is None:
        return LeadIdInfo(raw=None, canonical=None, is_explicit_dup=False, looks_valid=False)
    # An explicit "-dup" suffix (e.g. "L-1313-dup") is the export's own way
    # of flagging a duplicate submission -- record it, then strip it below
    # by simply not including it in the digit extraction.
    is_dup = bool(_DUP_SUFFIX_RE.search(s))
    # Pull out the first run of 2+ digits -- this is what actually
    # identifies the lead, regardless of "L-" prefix or "-dup" suffix.
    m = _LEAD_ID_DIGITS_RE.search(s)
    canonical = m.group(1) if m else None
    return LeadIdInfo(raw=s, canonical=canonical, is_explicit_dup=is_dup, looks_valid=canonical is not None)


# ---------------------------------------------------------------------------
# Row-level junk detection (embedded headers, blank rows, QA/test rows)
# ---------------------------------------------------------------------------


def is_blank_row(row: dict, columns: list[str]) -> bool:
    """True if every field in the row is empty/whitespace/NaN."""
    for col in columns:
        if _to_str(row.get(col)) is not None:
            return False
    return True


def is_header_echo_row(row: dict, columns: list[str]) -> bool:
    """True if a stray header row leaked into the data as a data row.

    Catches the exact case seen in real exports (a literal ``"header"``
    sentinel in the id column) as well as the general case where most
    non-empty cells just repeat their own column name.
    """
    # The exact case seen in this dataset: a literal "header" value sitting
    # in the lead_id column (the rest of that row is the column names,
    # shifted by one because the source header itself was malformed).
    lead_id_val = _to_str(row.get("lead_id"))
    if lead_id_val and lead_id_val.lower() in {"header", "lead_id"}:
        return True

    # General fallback for future exports: if at least half of a row's
    # non-empty cells are literally identical to their own column name
    # (e.g. cell "email" sitting in the "email" column), it's almost
    # certainly a header row that leaked into the data, not a real lead.
    matches = 0
    total = 0
    for col in columns:
        val = _to_str(row.get(col))
        if val is None:
            continue
        total += 1
        if val.lower() == col.lower():
            matches += 1
    return total > 0 and (matches / total) >= 0.5


_TEST_NOTE_MARKERS = ("qa test entry", "test test ignore", "please ignore")


def is_garbage_test_row(row: dict) -> bool:
    """True for obvious QA/placeholder rows (``TESTROW``, ``asdf``, ...)."""
    # A dedicated sentinel id used for QA rows.
    lead_id_val = _to_str(row.get("lead_id"))
    if lead_id_val and lead_id_val.lower() in {"testrow", "test"}:
        return True

    # Notes that explicitly say this row isn't real ("please ignore", etc.).
    notes = (_to_str(row.get("notes")) or "").lower()
    if any(marker in notes for marker in _TEST_NOTE_MARKERS):
        return True

    # A short repeated nonsense token across several unrelated fields (e.g.
    # name=company=title="asdf") is a classic keyboard-mash test row -- a
    # real lead would essentially never have identical name/company/title.
    sample_fields = ("name", "company", "title", "source")
    values = [(_to_str(row.get(f)) or "").lower() for f in sample_fields]
    values = [v for v in values if v]
    if len(values) >= 3 and len(set(values)) == 1 and values[0].isalpha() and len(values[0]) <= 6:
        return True

    return False
