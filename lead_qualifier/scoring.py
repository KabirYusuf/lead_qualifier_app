"""Lead qualification scoring.

Modeled on how an experienced SDR (sales development rep) triages an
inbound lead against an inferred ICP (ideal customer profile): assess
three independent things and sum them into one 0-100 score.

* **Fit (0-40 pts)** -- does this company/contact match the ICP? Vertical
  match (0-20), company-size fit (0-10), and title seniority (0-10). The
  ICP itself (vertical, size band, budget band, common pain points) was
  inferred empirically from the dataset's strongest-signal leads -- see
  ICP_SUMMARY below and the "Inferred ICP" section of README.md for the
  full derivation -- not assumed up front.

* **Intent (0-40 pts)** -- do the notes show genuine interest, urgency, or
  budget signals? Phrase-lexicon score from the notes text (0-30) plus the
  structured stated-budget field, also treated as a budget signal per the
  brief (0-10). If notes are blank or contain no recognizable signal at
  all, Intent is scored conservatively low rather than guessed at a
  neutral default -- see `_NO_SIGNAL_INTENT_POINTS` -- and the
  justification says so explicitly.

* **Engagement stage (0/10/20 pts)** -- cold contact, warm conversation, or
  sales-ready lead? Derived from *which* intent phrases matched (a
  commitment phrase like "budget approved" reads as sales-ready regardless
  of how many points it happens to carry), not from the point total.

Every component is independently clamped to its own nominal band, so
Fit + Intent + Engagement always lands in [0, 100] without needing a
separate final clip.

Two situations are hard disqualifiers, checked first and overriding
everything else: the notes match a non-prospect pattern (job seeker, spam,
investor, student, competitor, recruiter/vendor pitch, ...), or the lead
has no name, email, or company at all to act on. Both force score 0, which
already falls in the Disqualify band below.

Recommendation thresholds: score >= 65 -> Contact Now; 40-64 -> Nurture;
below 40 -> Disqualify. All weights/thresholds are named module-level
constants so a future maintainer can retune the model without touching the
control flow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .cleaning import clean_text

# ---------------------------------------------------------------------------
# Inferred ICP (Step 1) -- derived from the dataset itself, not assumed.
#
# Method: pulled every lead whose notes combine an explicit budget
# commitment ("budget approved" / "budgeted, serious") with an urgency
# phrase ("wants to start ASAP", "this is my priority to solve", ...) --
# 85 leads, the strongest-signal group independent of any scoring system --
# and looked at what they have in common.
#
# Vertical:      marketing/growth/sales-enablement agencies -- social media,
#                cold email, SEO, influencer marketing, full-service
#                marketing, outbound, lead gen, performance marketing, GTM,
#                growth, PPC, email marketing, appointment-setting, content,
#                and demand-gen agencies all appear repeatedly; no single
#                narrow niche dominates, the common thread is "agency
#                running marketing/growth operations for clients."
# Company size:  4-80 employees, median 44, IQR 29-58 -- a mid-size
#                operating team, not a solo shop and not an enterprise.
# Budget:        $4,000-$18,000/mo stated, median $8,500/mo, IQR
#                $7,000-$11,000/mo.
# Titles:        ops/growth leadership and owners -- Head of RevOps,
#                Managing Director, Owner, CEO, COO, Head of Ops, Head of
#                Growth, Managing Partner, VP Ops/Growth. Decision-makers or
#                senior operational leadership, not individual contributors.
# Pain points:   repetitive, manual, cross-tool operational busywork --
#                enriching/scoring leads one by one, moving leads between
#                Apollo and the CRM by hand, drafting first-touch messages,
#                manual lead routing, pacing ad budgets across accounts,
#                chasing follow-ups across email/WhatsApp, triaging a
#                shared inbox, qualifying inbound leads, building client
#                reports across tools, summarizing call recordings.
# Geography:     mixed -- website TLDs skew toward .co/.agency/.io alongside
#                a substantial .ng/.africa share, suggesting a customer base
#                spanning Africa and other English-speaking global markets
#                rather than one single region (TLD is a proxy, not proof).
# ---------------------------------------------------------------------------

ICP_SUMMARY = (
    "Marketing/growth agencies (any sub-vertical: SEO, social, cold email, "
    "PPC, demand gen, etc.), roughly 4-80 employees (median ~44), with a "
    "monthly budget of $4,000-$18,000 (median ~$8,500), run by ops/growth "
    "leadership or an owner, whose pain is repetitive manual work across "
    "lead handling, CRM data entry, reporting, or client comms -- spanning "
    "African and other global English-speaking markets."
)

# ---------------------------------------------------------------------------
# Hard disqualifiers -- matched first; if any hit, the lead is out
# regardless of everything else. Both cases force score 0, which already
# sits in the Disqualify band (see qualify_lead()).
# ---------------------------------------------------------------------------

HARD_DISQUALIFY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"looking for a role|attaching (my )?cv|are you hiring|join your team", re.I), "job_seeker"),
    (re.compile(r"\bvc here\b|portfolio compan|not a direct buyer", re.I), "investor_not_buyer"),
    (re.compile(r"\bcs student\b|final year student|university project|bootcamp grad|how you built your systems", re.I), "student_researcher"),
    (re.compile(r"\bjournalist\b|looking for a quote", re.I), "press_inquiry"),
    (re.compile(r"competing automation agency|fellow agency owner|researching the market|do similar work|pricing for benchmarking", re.I), "competitor"),
    (re.compile(r"automation devs on our bench|place candidates", re.I), "recruiter_pitch"),
    (re.compile(r"offshore dev team|bulk email blasting|\$\d+\s*/\s*hr\b", re.I), "vendor_pitch"),
    (re.compile(r"smm panel|buy followers|high-da backlinks|reply stop|you have won|click here to claim", re.I), "spam"),
    (re.compile(r"newsletter signup.*mistake", re.I), "mistake_entry"),
    (re.compile(r"qa test entry|ignore this", re.I), "test_data"),
]

HARD_DISQUALIFY_LABELS = {
    "job_seeker": "job seeker, not a buyer",
    "investor_not_buyer": "investor/VC, not a direct buyer",
    "student_researcher": "student/academic research, not a client",
    "press_inquiry": "press inquiry, not a client",
    "competitor": "competitor researching the market",
    "recruiter_pitch": "recruiter pitching candidates (not a lead)",
    "vendor_pitch": "vendor pitching us a service (not a lead)",
    "spam": "spam / scam submission",
    "mistake_entry": "submitted by mistake (newsletter signup)",
    "test_data": "QA/test placeholder row",
    "insufficient_contact_data": "no name, email, or company to act on",
}

# ---------------------------------------------------------------------------
# Intent lexicon: (pattern, weight, tag, stage). Positive weight = stronger
# buying intent; negative weight = a stated reason contact isn't ready yet.
# `stage` is this phrase's contribution to Engagement Stage -- kept on the
# same row as its weight so the two never drift out of sync. Weights sum
# into a 0-30 notes-phrase band (clamped in score_notes_intent()); the
# remaining 10 of Intent's 40 points come from the stated-budget field.
# ---------------------------------------------------------------------------

SALES_READY = "sales_ready"
WARM = "warm"
COLD = "cold"

INTENT_SIGNAL_PATTERNS: list[tuple[re.Pattern, float, str, str]] = [
    # Strong positive signals -- these are what "sales-ready" means.
    (re.compile(r"budget approved", re.I), 14.0, "budget_approved", SALES_READY),
    (re.compile(r"budgeted,?\s*serious", re.I), 12.0, "budgeted_serious", SALES_READY),
    (re.compile(r"wants to start asap|keen to move fast|ready to pilot in the next 2 weeks|"
                r"this is my priority to solve|decision this month|wants to move in 2 weeks|"
                r"priority for the quarter", re.I), 7.0, "urgency", SALES_READY),
    (re.compile(r"i make the call here|decision is mine", re.I), 5.0, "confirmed_authority", SALES_READY),
    # Real interest, not yet a firm commitment -- a warm conversation.
    (re.compile(r"eating our week|want it automated end to end", re.I), 5.0, "clear_pain_point", WARM),
    (re.compile(r"(?<!no )\breal budget\b", re.I), 6.0, "real_budget", WARM),
    (re.compile(r"money to spend", re.I), 4.0, "money_to_spend", WARM),
    (re.compile(r"have some budget", re.I), 3.0, "some_budget", WARM),
    (re.compile(r"comparing a few options", re.I), 2.0, "actively_comparing", WARM),
    (re.compile(r"not totally sure what we need yet|interested but vague on scope", re.I), -3.0, "scope_unclear", WARM),
    (re.compile(r"price sensitive", re.I), -3.0, "price_sensitive", WARM),
    (re.compile(r"budget not locked yet", re.I), -3.0, "budget_not_locked", WARM),
    (re.compile(r"would need to loop in the team", re.I), -3.0, "needs_internal_buyin", WARM),
    (re.compile(r"not sure who signs off internally", re.I), -4.0, "authority_unclear", WARM),
    (re.compile(r"wont share budget yet|won.?t share budget|depends what you can do", re.I), -3.0, "budget_withheld", WARM),
    # Explicit low/no budget -- a cold contact, not just an early one.
    (re.compile(r"no real budget yet", re.I), -4.0, "no_budget_yet", COLD),
    (re.compile(r"tiny budget|one-man shop", re.I), -4.0, "tiny_budget", COLD),
    (re.compile(r"budget way below range", re.I), -7.0, "budget_below_range", COLD),
    (re.compile(r"can.?t really pay right now", re.I), -6.0, "cannot_pay_now", COLD),
]

INTENT_TAG_LABELS = {
    "budget_approved": "budget approved",
    "budgeted_serious": "budgeted and serious",
    "clear_pain_point": "a clear, specific pain point",
    "urgency": "urgency to start",
    "confirmed_authority": "confirmed decision-making authority",
    "real_budget": "a real budget confirmed",
    "money_to_spend": "money to spend",
    "some_budget": "some budget",
    "actively_comparing": "actively comparing options",
    "scope_unclear": "scope not yet defined",
    "price_sensitive": "price sensitivity",
    "budget_not_locked": "budget not locked yet",
    "needs_internal_buyin": "a need for internal buy-in",
    "authority_unclear": "no decision-maker identified yet",
    "budget_withheld": "budget undisclosed",
    "no_budget_yet": "no budget yet (early-stage)",
    "tiny_budget": "a very small budget",
    "budget_below_range": "a budget well below our range",
    "cannot_pay_now": "an inability to pay right now",
}

# Notes that are blank, or contain no phrase this lexicon recognizes, can't
# be assessed for intent -- score conservatively low rather than guessing
# or defaulting to a flattering neutral value (explicit instruction: don't
# inflate an unknown into a "maybe"). Out of the 0-30 notes-phrase band.
_NO_SIGNAL_INTENT_POINTS = 3.0

# Stated (structured) monthly budget field is also a budget SIGNAL, per the
# brief's own definition of Intent ("genuine interest, urgency, or BUDGET
# SIGNALS") -- it lives in Intent, not Fit, even though it's a structured
# column rather than free text. Bands are informed by the inferred ICP's
# observed budget range ($4,000-$18,000/mo, median $8,500).
_STATED_BUDGET_INTENT_BANDS = (
    (8000.0, 10.0),   # at/above the ICP median -- strong signal
    (4000.0, 7.0),    # within the ICP's observed range, below median
    (1000.0, 3.0),    # below the typical ICP range but non-zero
    (0.0, 0.0),        # explicit $0
)
_STATED_BUDGET_UNKNOWN_POINTS = 2.0  # not stated -- conservative, not neutral


def score_stated_budget_intent(budget: float | None) -> float:
    """Score the structured monthly_budget field as an Intent signal (0-10 pts)."""
    if budget is None:
        return _STATED_BUDGET_UNKNOWN_POINTS
    for floor, points in _STATED_BUDGET_INTENT_BANDS:
        if budget >= floor:
            return points
    return 0.0


def _raw_notes_intent_sum(tags: set[str]) -> float:
    """Sum of matched notes-phrase weights, UNCLAMPED.

    Internal building block: score_notes_intent() clamps this to the public
    0-30 band, but qualify_lead() also needs the unclamped figure (to
    compute raw_score -- see QualificationResult) so stacking many positive
    tags past the nominal cap still shows up as "genuinely stronger" for
    tie-breaking, even though the displayed Intent component saturates.
    """
    return sum(weight for _pattern, weight, tag, _stage in INTENT_SIGNAL_PATTERNS if tag in tags)


def score_notes_intent(tags: set[str]) -> float:
    """Sum matched notes-phrase weights, clamped to the 0-30 nominal band.

    An empty tag set (blank notes, or notes with no recognizable phrase)
    returns the conservative-low floor rather than 0 or a neutral value --
    "too vague to assess" is treated as a known-weak signal, not scored as
    if it were either the best or worst possible answer.
    """
    if not tags:
        return _NO_SIGNAL_INTENT_POINTS
    return max(0.0, min(30.0, _raw_notes_intent_sum(tags)))


_NOT_AGENCY_RE = re.compile(r"not an agency", re.I)
_AGENCY_RE = re.compile(r"\bagency\b", re.I)
_ADJACENT_ICP_RE = re.compile(r"saas company|ecom brand", re.I)


@dataclass
class NoteSignals:
    """Everything classify_notes() extracted from one lead's notes text."""
    text: str  # lower-cased, whitespace-cleaned notes (empty string if blank)
    hard_disqualify: bool  # True if any HARD_DISQUALIFY_PATTERNS matched
    disqualify_reasons: list[str] = field(default_factory=list)  # tag(s) that matched, if hard_disqualify
    tags: set[str] = field(default_factory=set)  # which intent signals matched, for the justification sentence
    stage: str = COLD  # SALES_READY / WARM / COLD -- see classify_engagement_stage()
    is_agency_icp: bool = False  # notes mention "agency" (our ideal customer profile)
    is_adjacent_icp: bool = False  # notes mention a related-but-not-agency business (SaaS/ecom/"not an agency")
    notes_too_vague: bool = False  # blank, or no phrase in the lexicon matched -- Intent scored conservatively


def classify_engagement_stage(tags: set[str]) -> str:
    """Derive Cold / Warm / Sales-Ready from *which* intent phrases matched.

    Not a function of the point total -- a lead can score modestly on
    points but still read as "sales-ready" the moment it contains a genuine
    commitment phrase (budget approved, confirmed authority, urgency). The
    highest stage reached by any single matched tag wins, since one strong
    signal outweighs several weak ones for readiness purposes.
    """
    tag_to_stage = {tag: stage for _pattern, _weight, tag, stage in INTENT_SIGNAL_PATTERNS}
    stages_present = {tag_to_stage[t] for t in tags if t in tag_to_stage}
    if SALES_READY in stages_present:
        return SALES_READY
    if WARM in stages_present:
        return WARM
    return COLD  # either explicit cold signals, or no signal at all


def classify_notes(raw_notes) -> NoteSignals:
    """Classify a notes field into disqualify reasons, intent tags, and stage.

    Runs in two stages: first check whether the note matches any hard
    disqualifier (job seeker, spam, etc.) -- if so, stop immediately and
    don't bother scoring intent, since the lead is out regardless. Only if
    it clears that check do we scan for positive/negative intent phrases,
    derive the engagement stage, and detect the ICP (agency / adjacent /
    neither).
    """
    text = (clean_text(raw_notes) or "").lower()
    if not text:
        # No notes at all -- can't assess intent; flag it rather than guess.
        return NoteSignals(text="", hard_disqualify=False, stage=COLD, notes_too_vague=True)

    # Stage 1: hard disqualifiers. Collect every matching reason (a note
    # could in theory match more than one pattern) for a fuller audit trail,
    # but a single match is enough to short-circuit -- no intent scoring
    # matters once we know this isn't a real sales opportunity.
    disqualify_reasons = [reason for pattern, reason in HARD_DISQUALIFY_PATTERNS if pattern.search(text)]
    if disqualify_reasons:
        return NoteSignals(text=text, hard_disqualify=True, disqualify_reasons=disqualify_reasons)

    # Stage 2: intent lexicon -- which phrases matched (weights are summed
    # separately in score_notes_intent(), kept apart from tag detection so
    # the same tag set can also drive engagement stage and the ICP checks
    # below without recomputing).
    tags = {tag for pattern, _weight, tag, _stage in INTENT_SIGNAL_PATTERNS if pattern.search(text)}
    stage = classify_engagement_stage(tags)

    # ICP (ideal customer profile) detection. "not an agency" must be
    # checked before the bare "agency" pattern, since the word "agency"
    # still appears inside that phrase -- without this ordering a lead who
    # explicitly says they're NOT an agency would be mis-tagged as one.
    if _NOT_AGENCY_RE.search(text):
        is_agency, is_adjacent = False, True
    elif _AGENCY_RE.search(text):
        is_agency, is_adjacent = True, False
    elif _ADJACENT_ICP_RE.search(text):
        is_agency, is_adjacent = False, True
    else:
        is_agency, is_adjacent = False, False

    return NoteSignals(
        text=text,
        hard_disqualify=False,
        tags=tags,
        stage=stage,
        is_agency_icp=is_agency,
        is_adjacent_icp=is_adjacent,
        notes_too_vague=(not tags),  # had text, but none of it matched anything recognizable
    )


# ---------------------------------------------------------------------------
# Fit scoring (0-40 pts): does this company/contact match the inferred ICP?
# Vertical match (0-20) + company-size fit (0-10) + title seniority (0-10).
# ---------------------------------------------------------------------------

_DECISION_TITLE_KEYWORDS = ("ceo", "cto", "coo", "founder", "owner", "managing director", "managing partner", "president", "partner")
_INFLUENCER_TITLE_KEYWORDS = ("vp", "head of", "director")
_LOW_AUTHORITY_TITLE_KEYWORDS = ("student", "freelancer", "developer", "recruiter", "intern")


def _any_keyword_matches(text: str, keywords: tuple[str, ...]) -> bool:
    """Whole-word/phrase keyword match -- avoids substrings like "cto" inside
    "director" or "coo" inside "director of ops" false-positiving."""
    return any(re.search(rf"\b{re.escape(k)}\b", text) for k in keywords)


def score_title(title: str | None) -> tuple[float, str]:
    """Score title-based decision-making authority (0-10 pts). Returns (score, tier)."""
    if not title:
        return 4.0, "unknown"
    t = title.lower().strip()
    if _any_keyword_matches(t, _LOW_AUTHORITY_TITLE_KEYWORDS):
        return 0.0, "low_authority"
    if _any_keyword_matches(t, _DECISION_TITLE_KEYWORDS):
        return 10.0, "decision_maker"
    if _any_keyword_matches(t, _INFLUENCER_TITLE_KEYWORDS):
        return 7.0, "influencer"
    return 4.0, "other"


def score_employees(employees: float | None) -> float:
    """Score company headcount against the ICP's observed size band (0-10 pts).

    The inferred ICP's strongest-signal leads run 4-80 employees (median
    44) -- so that full range counts as a good fit; a 1-2 person shop is
    below it, and unknown headcount is scored conservatively rather than
    assumed to fit.
    """
    if employees is None:
        return 4.0
    if employees >= 3:
        return 10.0
    return 2.0


def score_icp(is_agency_icp: bool, is_adjacent_icp: bool) -> float:
    """Score vertical match to the inferred ICP -- a marketing/growth
    agency, any sub-type (0-20 pts).

    A direct agency match scores full marks; an adjacent business (SaaS/
    ecom that explicitly wants similar help) scores half credit; anything
    else contributes nothing (not negative -- we just don't reward it).
    """
    if is_agency_icp:
        return 20.0
    if is_adjacent_icp:
        return 10.0
    return 0.0


# ---------------------------------------------------------------------------
# Overall qualification
# ---------------------------------------------------------------------------

CONTACT_NOW_THRESHOLD = 65.0
NURTURE_THRESHOLD = 40.0  # 40-64 -> Nurture; below 40 -> Disqualify.

# Kept for backwards-compatible imports (cli.py / app.py / tests may still
# reference these default-threshold names).
DEFAULT_CONTACT_NOW_THRESHOLD = CONTACT_NOW_THRESHOLD
DEFAULT_NURTURE_THRESHOLD = NURTURE_THRESHOLD

CONTACT_NOW = "Contact Now"
NURTURE = "Nurture"
DISQUALIFY = "Disqualify"

STAGE_LABELS = {SALES_READY: "Sales-Ready", WARM: "Warm", COLD: "Cold"}
_STAGE_SCORE_POINTS = {SALES_READY: 20.0, WARM: 10.0, COLD: 0.0}


@dataclass
class QualificationResult:
    """The final verdict for one lead, plus the three SDR-framework
    components that produced it (kept separate from the total so the
    pipeline/dashboard can display or debug Fit/Intent/Stage independently,
    not just the combined number)."""
    score: float  # Fit + Intent + Engagement, 0-100 (0 whenever hard_disqualify is True)
    raw_score: float  # same components, but BEFORE each is clamped to its nominal band -- can exceed 100 or go negative.
    # Kept alongside the clipped `score` purely so callers can break ties between
    # leads that land on the same displayed score without losing information
    # about which one was actually stronger (see pipeline.py's ranking sort).
    recommendation: str  # one of CONTACT_NOW / NURTURE / DISQUALIFY
    hard_disqualify: bool  # True if a non-prospect pattern fired, or there's no contact info at all
    justification: str  # one concise, SDR-style sentence explaining the verdict
    fit_score: float  # 0-40 pts: ICP vertical match + company-size fit + title seniority
    intent_score: float  # 0-40 pts: notes-phrase signal + stated-budget signal
    stage_score: float  # 0/10/20 pts, from engagement_stage
    engagement_stage: str  # "Cold" / "Warm" / "Sales-Ready"
    title_tier: str  # "decision_maker" / "influencer" / "low_authority" / "other" / "unknown"
    is_agency_icp: bool
    is_adjacent_icp: bool
    notes_too_vague: bool  # notes were blank or matched nothing -- Intent was scored conservatively, not guessed


def _build_justification(
    *, recommendation: str, fit_label: str, title_tier: str,
    tags: set[str], stage_label: str, notes_too_vague: bool,
) -> str:
    """Compose the single-sentence, SDR-style justification for a lead's verdict."""
    if notes_too_vague:
        vague_clause = "notes are blank/too vague to assess intent, scored conservatively"
        return f"{fit_label.capitalize()}; {vague_clause}; {stage_label.lower()} -- {recommendation}."

    title_phrase = {
        "decision_maker": "decision-maker authority",
        "influencer": "influencer authority",
        "low_authority": "low authority",
    }.get(title_tier)
    fit_bits = [fit_label] + ([title_phrase] if title_phrase else [])
    fit_clause = " with ".join(fit_bits) if len(fit_bits) > 1 else fit_bits[0]

    # Prefer naming the strongest 1-2 signals actually present over a
    # generic phrase -- pick sales-ready tags first, then warm, in the
    # lexicon's own declared order so the sentence reads deterministically.
    ordered_tags = [tag for _p, _w, tag, _s in INTENT_SIGNAL_PATTERNS if tag in tags]
    if ordered_tags:
        intent_clause = " and ".join(INTENT_TAG_LABELS[t] for t in ordered_tags[:2])
    else:
        intent_clause = "no strong signals in the notes"

    return f"{fit_clause.capitalize()}; {intent_clause}; {stage_label.lower()} -- {recommendation}."


def qualify_lead(
    *,
    notes,
    title,
    employees: float | None,
    budget: float | None,
    has_name: bool,
    has_email: bool,
    has_company: bool,
    contact_now_threshold: float = CONTACT_NOW_THRESHOLD,
    nurture_threshold: float = NURTURE_THRESHOLD,
) -> QualificationResult:
    """Score a single (already-cleaned) lead like an experienced SDR would:
    Fit (0-40) + Intent (0-40) + Engagement Stage (0/10/20) -> a 0-100
    score, a recommendation, and a one-line justification.

    Expects fields that have already been through lead_qualifier.cleaning
    (e.g. ``employees``/``budget`` as parsed floats, not raw strings).

    Two situations short-circuit straight to Disqualify with score 0 before
    any of the three components are scored: no name/email/company at all
    (nothing to act on), or notes matching a non-prospect pattern (job
    seeker, spam, investor, student, competitor, ...). Otherwise:
    score >= contact_now_threshold -> Contact Now;
    score >= nurture_threshold -> Nurture; else -> Disqualify.
    """
    note_signals = classify_notes(notes)

    # Hard-disqualifier: nothing to act on, regardless of notes.
    if not (has_name or has_email or has_company):
        return QualificationResult(
            score=0.0, raw_score=0.0, recommendation=DISQUALIFY, hard_disqualify=True,
            justification=f"{HARD_DISQUALIFY_LABELS['insufficient_contact_data'].capitalize()} -- {DISQUALIFY}.",
            fit_score=0.0, intent_score=0.0, stage_score=0.0, engagement_stage=STAGE_LABELS[COLD],
            title_tier="unknown", is_agency_icp=False, is_adjacent_icp=False, notes_too_vague=False,
        )

    # Hard-disqualifier: a non-prospect pattern in the notes.
    if note_signals.hard_disqualify:
        reason_text = "; ".join(HARD_DISQUALIFY_LABELS.get(r, r) for r in note_signals.disqualify_reasons)
        return QualificationResult(
            score=0.0, raw_score=0.0, recommendation=DISQUALIFY, hard_disqualify=True,
            justification=f"{reason_text.capitalize()} -- {DISQUALIFY}.",
            fit_score=0.0, intent_score=0.0, stage_score=0.0, engagement_stage=STAGE_LABELS[COLD],
            title_tier="unknown", is_agency_icp=False, is_adjacent_icp=False, notes_too_vague=False,
        )

    # Fit (0-40): ICP vertical match + company-size fit + title seniority.
    title_score, title_tier = score_title(title)
    fit_raw = title_score + score_employees(employees) + score_icp(note_signals.is_agency_icp, note_signals.is_adjacent_icp)
    fit_score = max(0.0, min(40.0, fit_raw))

    # Intent (0-40): notes-phrase signal (0-30, conservative-low if the
    # notes can't be assessed) + stated-budget signal (0-10). Uses the
    # UNCLAMPED notes-phrase sum here (not score_notes_intent(), which
    # already clamps to 0-30) so intent_raw genuinely preserves precision
    # past the nominal cap for raw_score/tie-breaking below.
    notes_intent_raw = _NO_SIGNAL_INTENT_POINTS if note_signals.notes_too_vague else _raw_notes_intent_sum(note_signals.tags)
    stated_budget_intent = score_stated_budget_intent(budget)
    intent_raw = notes_intent_raw + stated_budget_intent
    intent_score = max(0.0, min(40.0, intent_raw))

    # Engagement stage (0/10/20): from *which* phrases matched, not the point total.
    stage_score = _STAGE_SCORE_POINTS[note_signals.stage]
    engagement_stage = STAGE_LABELS[note_signals.stage]

    raw_total = fit_raw + intent_raw + stage_score
    total = fit_score + intent_score + stage_score  # each component already clamped -> naturally in [0, 100]

    if total >= contact_now_threshold:
        recommendation = CONTACT_NOW
    elif total >= nurture_threshold:
        recommendation = NURTURE
    else:
        recommendation = DISQUALIFY

    fit_label = (
        "agency ICP fit" if note_signals.is_agency_icp
        else "adjacent-market fit" if note_signals.is_adjacent_icp
        else "off-ICP"
    )
    justification = _build_justification(
        recommendation=recommendation, fit_label=fit_label, title_tier=title_tier,
        tags=note_signals.tags, stage_label=engagement_stage,
        notes_too_vague=note_signals.notes_too_vague,
    )

    return QualificationResult(
        score=total, raw_score=raw_total, recommendation=recommendation, hard_disqualify=False,
        justification=justification, fit_score=fit_score, intent_score=intent_score,
        stage_score=stage_score, engagement_stage=engagement_stage, title_tier=title_tier,
        is_agency_icp=note_signals.is_agency_icp, is_adjacent_icp=note_signals.is_adjacent_icp,
        notes_too_vague=note_signals.notes_too_vague,
    )
