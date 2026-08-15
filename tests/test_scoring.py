"""Unit tests for lead_qualifier.scoring.

All expected values in this file were computed empirically against the
actual implementation (see the analysis in the project's build history)
rather than hand-calculated, to avoid baking in arithmetic mistakes.
"""

import pytest

from lead_qualifier import scoring


# ---------------------------------------------------------------------------
# classify_notes -- hard disqualifiers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("notes,expected_reason", [
    ("Not looking to buy — I'm a developer looking for a role. Attaching my CV.", "job_seeker"),
    ("Are you hiring developers? I'd love to join your team.", "job_seeker"),
    ("VC here — wanting to intro you to a few portfolio companies. Not a direct buyer.", "investor_not_buyer"),
    ("hi! CS student, i love what you do. could you send a free template or resources?", "student_researcher"),
    ("final year student doing a project on AI automation, can you share how you built your systems?", "student_researcher"),
    ("Journalist writing about the AI automation space, looking for a quote.", "press_inquiry"),
    ("I actually run a competing automation agency, just seeing how you package your offer.", "competitor"),
    ("Fellow agency owner here, mostly researching the market.", "competitor"),
    ("We have automation devs on our bench, would love to place candidates with you.", "recruiter_pitch"),
    ("Offering offshore dev team at $5/hr, interested?", "vendor_pitch"),
    ("Cheap SMM panel, buy followers and likes, DM for rates.", "spam"),
    ("You have WON $1,000,000!!! Click here to claim.", "spam"),
    ("This is a newsletter signup that ended up in the leads sheet by mistake.", "mistake_entry"),
    ("QA test entry, please ignore.", "test_data"),
])
def test_hard_disqualify_patterns(notes, expected_reason):
    signals = scoring.classify_notes(notes)
    assert signals.hard_disqualify is True
    assert expected_reason in signals.disqualify_reasons


def test_hot_lead_note_is_not_hard_disqualified():
    notes = (
        "We're a influencer marketing agency, 26 people. Chasing follow-ups across "
        "email and whatsapp is eating our week. Want it automated end to end. "
        "Budget approved, wants to start ASAP."
    )
    signals = scoring.classify_notes(notes)
    assert signals.hard_disqualify is False


def test_blank_notes_no_disqualify_no_signals_but_flagged_too_vague():
    signals = scoring.classify_notes("")
    assert signals.hard_disqualify is False
    assert signals.tags == set()
    assert signals.notes_too_vague is True


def test_notes_with_no_recognizable_phrase_are_also_flagged_too_vague():
    # Has real text, but nothing this lexicon recognizes -- must not be
    # silently treated the same as a strong or neutral signal.
    signals = scoring.classify_notes("Reached out on a Tuesday afternoon.")
    assert signals.hard_disqualify is False
    assert signals.tags == set()
    assert signals.notes_too_vague is True


# ---------------------------------------------------------------------------
# classify_notes -- intent tags and score_notes_intent()
# ---------------------------------------------------------------------------

def test_budget_approved_and_urgency_are_strongly_positive():
    signals = scoring.classify_notes(
        "Budget approved, keen to move fast. Want it automated end to end."
    )
    assert {"budget_approved", "urgency", "clear_pain_point"} <= signals.tags
    assert signals.stage == scoring.SALES_READY
    assert scoring.score_notes_intent(signals.tags) == 26.0  # 14 + 7 + 5


def test_no_budget_yet_is_negative_and_clamped_at_zero():
    signals = scoring.classify_notes(
        "very early startup, 3 people, no real budget yet but sharp and might grow."
    )
    assert "no_budget_yet" in signals.tags
    assert signals.stage == scoring.COLD
    # Raw weight is -4, but score_notes_intent() clamps its nominal band to
    # [0, 30] -- Intent never reports a negative component.
    assert scoring.score_notes_intent(signals.tags) == 0.0


def test_comparing_options_is_mildly_positive_and_warm():
    signals = scoring.classify_notes(
        "Interested in automating chasing follow-ups. Comparing a few options."
    )
    assert "actively_comparing" in signals.tags
    assert signals.stage == scoring.WARM
    assert scoring.score_notes_intent(signals.tags) == 2.0


def test_a_single_sales_ready_tag_wins_the_stage_even_with_other_warm_tags():
    signals = scoring.classify_notes(
        "Comparing a few options, but budget approved and ready to pilot in the next 2 weeks."
    )
    assert signals.stage == scoring.SALES_READY


# ---------------------------------------------------------------------------
# classify_notes -- ICP detection
# ---------------------------------------------------------------------------

def test_agency_mention_sets_agency_icp():
    signals = scoring.classify_notes("We're a SEO agency, 40 people. Budget approved.")
    assert signals.is_agency_icp is True
    assert signals.is_adjacent_icp is False


def test_not_an_agency_does_not_count_as_agency_icp():
    signals = scoring.classify_notes(
        "SaaS company (not an agency) that wants AI ops help. Budgeted, serious."
    )
    assert signals.is_agency_icp is False
    assert signals.is_adjacent_icp is True


def test_no_icp_mention_is_neither():
    signals = scoring.classify_notes("Looking into automating triaging a shared inbox.")
    assert signals.is_agency_icp is False
    assert signals.is_adjacent_icp is False


def test_off_icp_and_budget_incompatible_lands_in_disqualify_on_score_alone():
    # No special-cased "fit" override needed here -- weak/negative signals
    # on every axis (off-ICP-ish fit, a cold-tier budget signal, no stage
    # momentum) are already enough to fall below the Disqualify threshold
    # through ordinary scoring.
    result = scoring.qualify_lead(
        notes="small local business, not an agency, wants a cheap chatbot. budget way below range.",
        title="Owner", employees=6.0, budget=500.0,
        has_name=True, has_email=True, has_company=True,
    )
    assert result.recommendation == scoring.DISQUALIFY
    assert result.hard_disqualify is False  # disqualified on score, not a non-prospect pattern


# ---------------------------------------------------------------------------
# Fit component scoring
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected_tier", [
    ("CEO", "decision_maker"),
    ("Founder", "decision_maker"),
    ("Owner", "decision_maker"),
    ("Managing Director", "decision_maker"),
    ("VP Growth", "influencer"),
    ("Head of Ops", "influencer"),
    ("Director of Ops", "influencer"),
    ("Student", "low_authority"),
    ("Freelancer", "low_authority"),
    ("Developer", "low_authority"),
    (None, "unknown"),
    ("", "unknown"),
    ("Consultant", "other"),
])
def test_score_title_tiers(title, expected_tier):
    _, tier = scoring.score_title(title)
    assert tier == expected_tier


def test_score_employees_unknown_is_conservative_not_generous():
    assert scoring.score_employees(None) == 4.0
    assert scoring.score_employees(1) < scoring.score_employees(None)
    assert scoring.score_employees(50) > scoring.score_employees(None)


# ---------------------------------------------------------------------------
# Intent component: stated (structured) budget field
# ---------------------------------------------------------------------------

def test_score_stated_budget_intent_unknown_is_conservative_not_neutral():
    # Unknown budget is scored low, not a flattering "maybe" -- consistent
    # with the brief's instruction to never guess/inflate an unknown.
    assert scoring.score_stated_budget_intent(None) == 2.0
    assert scoring.score_stated_budget_intent(0) == 0.0
    assert scoring.score_stated_budget_intent(10000) > scoring.score_stated_budget_intent(2000)


# ---------------------------------------------------------------------------
# qualify_lead -- full integration
# ---------------------------------------------------------------------------

def test_hot_lead_is_contact_now():
    result = scoring.qualify_lead(
        notes=(
            "We're a SEO agency, 43 people. Moving leads between apollo and the "
            "crm by hand is eating our week. Want it automated end to end. "
            "Budget approved, want to start this month. This is my priority to solve."
        ),
        title="Director of Ops",
        employees=43.0,
        budget=7000.0,
        has_name=True, has_email=True, has_company=True,
    )
    assert result.recommendation == scoring.CONTACT_NOW
    assert result.hard_disqualify is False
    assert result.score >= scoring.CONTACT_NOW_THRESHOLD
    assert result.engagement_stage == "Sales-Ready"


def test_components_sum_to_total_score():
    result = scoring.qualify_lead(
        notes="We're a SEO agency, 43 people. Budget approved. This is my priority to solve.",
        title="Director of Ops", employees=43.0, budget=7000.0,
        has_name=True, has_email=True, has_company=True,
    )
    assert result.fit_score + result.intent_score + result.stage_score == result.score


def test_each_component_is_within_its_nominal_band():
    result = scoring.qualify_lead(
        notes="We're a SEO agency, 9 people. Budget approved. This is my priority to solve. I make the call here.",
        title="CEO", employees=9.0, budget=18000.0,
        has_name=True, has_email=True, has_company=True,
    )
    assert 0.0 <= result.fit_score <= 40.0
    assert 0.0 <= result.intent_score <= 40.0
    assert result.stage_score in (0.0, 10.0, 20.0)
    assert 0.0 <= result.score <= 100.0


def test_real_nurture_lead_lands_in_nurture_band():
    # A SaaS company (adjacent ICP, not agency) that's budgeted and serious
    # -- real interest and real budget, but an "other"-tier title and not
    # our core ICP, so it lands mid-funnel rather than Contact Now.
    result = scoring.qualify_lead(
        notes="SaaS company (not an agency) that wants AI ops help. Budgeted, serious.",
        title="Marketing Manager", employees=84.0, budget=6000.0,
        has_name=True, has_email=True, has_company=True,
    )
    assert result.recommendation == scoring.NURTURE
    assert scoring.NURTURE_THRESHOLD <= result.score < scoring.CONTACT_NOW_THRESHOLD


def test_job_seeker_is_disqualified_regardless_of_firmographics():
    result = scoring.qualify_lead(
        notes="Not looking to buy — I'm a developer looking for a role. Attaching my CV.",
        title="CEO",  # even a "strong" title can't rescue a non-lead
        employees=500.0,
        budget=50000.0,
        has_name=True, has_email=True, has_company=True,
    )
    assert result.recommendation == scoring.DISQUALIFY
    assert result.hard_disqualify is True
    assert result.score == 0.0


def test_no_contact_info_at_all_is_disqualified():
    result = scoring.qualify_lead(
        notes="follow up later??",
        title=None, employees=None, budget=None,
        has_name=False, has_email=False, has_company=False,
    )
    assert result.recommendation == scoring.DISQUALIFY
    assert result.hard_disqualify is True


def test_thin_but_real_lead_is_not_hard_disqualified():
    # Has a name/company but almost no other signal -- should be assessed
    # on its (low) merits, not auto-disqualified for missing data. It can
    # still land in Disqualify on score, but not via the hard-disqualify path.
    result = scoring.qualify_lead(
        notes="broken email. one line: 'call me'.",
        title=None, employees=None, budget=None,
        has_name=True, has_email=False, has_company=True,
    )
    assert result.hard_disqualify is False


def test_blank_notes_are_scored_conservatively_and_justification_says_so():
    result = scoring.qualify_lead(
        notes="",
        title="CEO", employees=50.0, budget=10000.0,
        has_name=True, has_email=True, has_company=True,
    )
    assert result.notes_too_vague is True
    assert "too vague" in result.justification.lower() or "blank" in result.justification.lower()


def test_custom_thresholds_change_bucket_not_score():
    kwargs = dict(
        notes="We're a SEO agency, 9 people. Budgeted, serious.",
        title="Head of RevOps", employees=9.0, budget=8000.0,
        has_name=True, has_email=True, has_company=True,
    )
    default_result = scoring.qualify_lead(**kwargs)
    strict_result = scoring.qualify_lead(**kwargs, nurture_threshold=200.0, contact_now_threshold=201.0)
    assert strict_result.recommendation == scoring.DISQUALIFY
    assert strict_result.score == default_result.score  # threshold changes bucket, not score


# ---------------------------------------------------------------------------
# raw_score (tie-break precision, preserved across the rubric change)
# ---------------------------------------------------------------------------

def test_raw_score_matches_score_when_no_component_is_clamped():
    result = scoring.qualify_lead(
        notes="SaaS company (not an agency) that wants AI ops help. Budgeted, serious.",
        title="COO", employees=None, budget=8000.0,
        has_name=True, has_email=True, has_company=True,
    )
    assert result.raw_score == result.score


def test_hard_disqualified_lead_has_zero_raw_score_too():
    result = scoring.qualify_lead(
        notes="Not looking to buy — I'm a developer looking for a role. Attaching my CV.",
        title="CEO", employees=500.0, budget=50000.0,
        has_name=True, has_email=True, has_company=True,
    )
    assert result.raw_score == 0.0
