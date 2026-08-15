"""End-to-end tests for lead_qualifier.pipeline against small synthetic CSVs.

Complements test_cleaning/test_scoring/test_dedup (which test units in
isolation) by checking the pieces actually work together: junk-row
filtering, deduplication, scoring, ranking, and file output.
"""

import os

import pandas as pd
import pytest

from lead_qualifier import scoring
from lead_qualifier.pipeline import load_raw_csv, run_pipeline, write_outputs

HEADER = "lead_id,created,name,email,company,employees,website,title,source,monthly_budget,notes\n"

SAMPLE_ROWS = [
    # A clear "Contact Now": agency ICP, budget approved, urgency, decision title.
    'L-1001,06/01/2024,Ada,ada@growthco.agency,GrowthCo,40,growthco.agency,CEO,webform,'
    '"$8,000/mo","We\'re a SEO agency, 40 people. Eating our week. '
    'Want it automated end to end. Budget approved, wants to start ASAP."\n',
    # A job seeker: hard disqualify regardless of anything else.
    'L-1002,06/02/2024,Sam,sam@example.com,,,,,webform,,'
    '"Not looking to buy — I\'m a developer looking for a role. Attaching my CV."\n',
    # A duplicate of L-1001 explicitly marked as such -- should be merged away.
    'L-1001,06/10/2024,Ada,ada@growthco.agency,GrowthCo,40,growthco.agency,CEO,webform,'
    '"$8,000/mo","(duplicate submission) We\'re a SEO agency, 40 people."\n',
    # A completely blank row.
    ',,,,,,,,,,\n',
    # A stray embedded header row.
    'header,lead_id,name,email,company,employees,website,title,source,budget,notes\n',
    # A QA/test placeholder row.
    'TESTROW,2024-06-06,Test User,test@test.com,Test,,test.com,test,test,,'
    '"QA test entry, please ignore."\n',
    # A mid-funnel nurture lead.
    'L-1003,06/05/2024,Tunde,tunde@midco.agency,MidCo,15,midco.agency,Head of Ops,linkedin,'
    '5000,"Interested in automating follow-ups. Comparing a few options. Budget not locked yet."\n',
]


@pytest.fixture()
def sample_csv(tmp_path):
    """Write SAMPLE_ROWS to a temp CSV file and return its path.

    pytest's tmp_path gives each test its own throwaway directory, so tests
    can run in parallel / any order without clobbering each other's files.
    """
    path = tmp_path / "leads.csv"
    path.write_text(HEADER + "".join(SAMPLE_ROWS), encoding="utf-8")
    return str(path)


class TestLoadRawCsv:
    def test_loads_all_columns_as_strings(self, sample_csv):
        df = load_raw_csv(sample_csv)
        assert list(df.columns) == [
            "lead_id", "created", "name", "email", "company",
            "employees", "website", "title", "source", "monthly_budget", "notes",
        ]
        # Every cell should come through as a plain Python str (no numeric/
        # date auto-coercion), regardless of pandas' internal string dtype.
        assert all(isinstance(v, str) for v in df["employees"])

    def test_missing_required_column_raises(self, tmp_path):
        bad_path = tmp_path / "bad.csv"
        bad_path.write_text("lead_id,name,email\nL-1,Ada,ada@x.com\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_raw_csv(str(bad_path))


class TestRunPipeline:
    def test_junk_rows_are_dropped(self, sample_csv):
        result = run_pipeline(sample_csv)
        # blank row + embedded header + TESTROW = 3 junk rows.
        assert result.junk_rows_dropped == 3

    def test_duplicate_is_merged_away(self, sample_csv):
        result = run_pipeline(sample_csv)
        assert result.duplicate_rows_dropped == 1
        assert (result.ranked["lead_id"] == "L-1001").sum() == 1

    def test_total_rows_read_matches_csv(self, sample_csv):
        result = run_pipeline(sample_csv)
        assert result.total_rows_read == len(SAMPLE_ROWS)

    def test_hot_lead_is_contact_now_and_ranked_first(self, sample_csv):
        result = run_pipeline(sample_csv)
        top = result.ranked.iloc[0]
        assert top["lead_id"] == "L-1001"
        assert top["recommendation"] == scoring.CONTACT_NOW

    def test_job_seeker_is_disqualified(self, sample_csv):
        result = run_pipeline(sample_csv)
        row = result.ranked[result.ranked["lead_id"] == "L-1002"].iloc[0]
        assert row["recommendation"] == scoring.DISQUALIFY

    def test_ranked_output_sorted_descending_by_score(self, sample_csv):
        result = run_pipeline(sample_csv)
        scores = result.ranked["score"].tolist()
        assert scores == sorted(scores, reverse=True)

    def test_qualified_count_matches_kept_rows(self, sample_csv):
        result = run_pipeline(sample_csv)
        # 7 rows - 3 junk - 1 duplicate = 3 real leads.
        assert result.qualified_rows == 3
        assert len(result.ranked) == 3

    def test_custom_thresholds_are_respected(self, sample_csv):
        # L-1003 is a mid-funnel lead that's "Disqualify" under default
        # thresholds (below the 40-point Nurture floor); a low enough bar
        # should still promote it all the way to "Contact Now".
        default = run_pipeline(sample_csv)
        default_row = default.ranked[default.ranked["lead_id"] == "L-1003"].iloc[0]
        assert default_row["recommendation"] == scoring.DISQUALIFY
        assert default_row["score"] > 0  # disqualified on score, not a hard-disqualify pattern

        loose = run_pipeline(sample_csv, contact_now_threshold=1.0, nurture_threshold=0.0)
        loose_row = loose.ranked[loose.ranked["lead_id"] == "L-1003"].iloc[0]
        assert loose_row["recommendation"] == scoring.CONTACT_NOW


class TestRawScoreTiebreak:
    """Two leads that both clip to a displayed score of 100 must still rank
    by which one is genuinely stronger (unclipped raw score), not by an
    arbitrary lead_id ordering -- see pipeline.py's sort_values call."""

    ROWS = [
        # Weaker of the two: both Fit and Intent independently max out at
        # their nominal caps (40 + 40 + 20 stage = displayed 100), but with
        # fewer stacked notes-phrases (raw ~108). Deliberately given the
        # numerically LOWER lead_id, so a naive lead_id tiebreak would
        # (wrongly) rank it first.
        'L-1000,06/01/2024,Ola,ola@weakco.agency,WeakCo,50,weakco.agency,CEO,webform,'
        '"$18,000/mo","We are a SEO agency. Budget approved. Budgeted, serious. '
        'I make the call here. Keen to move fast."\n',
        # Stronger: even more stacked notes-phrases past the same nominal
        # cap (raw ~128) -- displays the identical 100, but is genuinely
        # the stronger lead underneath. Given the numerically HIGHER
        # lead_id, so it should still rank first if raw-score tiebreak works.
        'L-9000,06/01/2024,Femi,femi@strongco.agency,StrongCo,50,strongco.agency,CEO,webform,'
        '"$18,000/mo","We are a SEO agency. Budget approved. Budgeted, serious. Keen to move '
        'fast. I make the call here. Want it automated end to end. Real budget. Money to '
        'spend. Have some budget. Comparing a few options."\n',
    ]

    @pytest.fixture()
    def tiebreak_csv(self, tmp_path):
        path = tmp_path / "tiebreak.csv"
        path.write_text(HEADER + "".join(self.ROWS), encoding="utf-8")
        return str(path)

    def test_both_leads_clip_to_the_same_displayed_score(self, tiebreak_csv):
        result = run_pipeline(tiebreak_csv)
        assert (result.ranked["score"] == 100.0).all()

    def test_genuinely_stronger_lead_ranks_first_despite_lower_lead_id_sorting_worse(self, tiebreak_csv):
        result = run_pipeline(tiebreak_csv)
        # If this were sorted by lead_id alone, "L-1000" would come before
        # "L-9000" alphanumerically -- the opposite of what should happen.
        assert result.ranked.iloc[0]["lead_id"] == "L-9000"
        assert result.ranked.iloc[1]["lead_id"] == "L-1000"


class TestWriteOutputs:
    def test_creates_parent_directories_and_files(self, sample_csv, tmp_path):
        result = run_pipeline(sample_csv)
        out_path = tmp_path / "nested" / "ranked.csv"
        excl_path = tmp_path / "nested" / "excluded.csv"
        write_outputs(result, str(out_path), str(excl_path))
        assert out_path.exists()
        assert excl_path.exists()

        written = pd.read_csv(out_path)
        assert len(written) == len(result.ranked)


def test_empty_input_produces_empty_but_well_formed_output(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text(HEADER, encoding="utf-8")
    result = run_pipeline(str(path))
    assert result.total_rows_read == 0
    assert result.ranked.empty
    assert list(result.ranked.columns) == [
        "lead_id", "created", "name", "email", "email_valid", "company", "website",
        "employees", "title", "source", "monthly_budget", "notes",
        "fit_score", "intent_score", "engagement_score", "score", "engagement_stage",
        "recommendation", "justification",
    ]
