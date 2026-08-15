"""Unit tests for lead_qualifier.dedup."""

from datetime import date

from lead_qualifier.dedup import dedupe_leads


def _rec(**overrides) -> dict:
    """Build a minimal fake "cleaned record" dict for dedupe_leads() tests.

    dedupe_leads() only touches a handful of keys (see completeness_fields,
    "notes", "created_date", the id fields) so a lightweight dict stand-in is
    enough -- no need to run the real cleaning pipeline for these tests.
    Pass keyword overrides to change just the fields a given test cares about.
    """
    base = {
        "lead_id_raw": "L-1000",
        "lead_id_canonical": "1000",
        "lead_id": "L-1000",
        "created_date": date(2024, 6, 1),
        "name": "Ola",
        "email": "ola@example.com",
        "company": "Example Co",
        "employees": 10.0,
        "website": "example.com",
        "title": "CEO",
        "source": "webform",
        "monthly_budget": 5000.0,
        "notes": "Budget approved.",
    }
    base.update(overrides)
    return base


def test_no_duplicates_returns_all_records_unchanged():
    records = [_rec(lead_id_canonical="1000"), _rec(lead_id_canonical="1001")]
    result = dedupe_leads(records)
    assert len(result.kept) == 2
    assert len(result.dropped) == 0


def test_explicit_dup_marker_is_dropped_in_favor_of_clean_copy():
    primary = _rec(lead_id_raw="L-1205", lead_id_canonical="1205", notes="We're a SEO agency.")
    dup = _rec(
        lead_id_raw="L-1205-dup", lead_id_canonical="1205",
        notes="(duplicate submission) We're a SEO agency.",
    )
    result = dedupe_leads([primary, dup])
    assert len(result.kept) == 1
    assert result.kept[0]["lead_id_raw"] == "L-1205"
    assert len(result.dropped) == 1
    assert result.dropped[0]["lead_id_raw"] == "L-1205-dup"
    assert result.dropped[0]["duplicate_of"] == "L-1205"


def test_id_formatting_differences_still_dedupe_together():
    # "L-1373" and "1373" share the same canonical id.
    a = _rec(lead_id_raw="1373", lead_id_canonical="1373", created_date=date(2024, 6, 16))
    b = _rec(lead_id_raw="1373", lead_id_canonical="1373", created_date=date(2024, 6, 27),
              notes="(duplicate submission) same lead")
    result = dedupe_leads([a, b])
    assert len(result.kept) == 1


def test_more_complete_record_wins_when_neither_marked_dup():
    sparse = _rec(lead_id_canonical="2000", website=None, title=None)
    complete = _rec(lead_id_canonical="2000", website="example.com", title="CEO")
    result = dedupe_leads([sparse, complete])
    assert result.kept[0]["title"] == "CEO"


def test_more_recent_record_wins_on_remaining_tie():
    older = _rec(lead_id_canonical="3000", created_date=date(2024, 6, 1))
    newer = _rec(lead_id_canonical="3000", created_date=date(2024, 6, 20))
    result = dedupe_leads([older, newer])
    assert result.kept[0]["created_date"] == date(2024, 6, 20)


def test_records_with_no_canonical_id_are_never_merged():
    a = _rec(lead_id_raw=None, lead_id_canonical=None)
    b = _rec(lead_id_raw=None, lead_id_canonical=None)
    result = dedupe_leads([a, b])
    assert len(result.kept) == 2
    assert len(result.dropped) == 0


def test_three_way_duplicate_group_keeps_exactly_one():
    records = [
        _rec(lead_id_raw="L-1", lead_id_canonical="1", notes="(duplicate submission) x"),
        _rec(lead_id_raw="L-1", lead_id_canonical="1", notes="original"),
        _rec(lead_id_raw="L-1-dup", lead_id_canonical="1", notes="(duplicate submission) y"),
    ]
    result = dedupe_leads(records)
    assert len(result.kept) == 1
    assert len(result.dropped) == 2
    assert result.kept[0]["notes"] == "original"
