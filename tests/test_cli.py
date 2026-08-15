"""Tests for the lead_qualifier CLI entry point."""

import pandas as pd
import pytest

from lead_qualifier.cli import main

HEADER = "lead_id,created,name,email,company,employees,website,title,source,monthly_budget,notes\n"
ROW = (
    'L-1001,06/01/2024,Ada,ada@growthco.agency,GrowthCo,40,growthco.agency,CEO,webform,'
    '"$8,000/mo","We\'re a SEO agency, 40 people. Eating our week. '
    'Want it automated end to end. Budget approved, wants to start ASAP."\n'
)


@pytest.fixture()
def sample_csv(tmp_path):
    """A single-lead CSV (one clear 'Contact Now') written to a temp file,
    used to exercise main() without depending on the real dataset."""
    path = tmp_path / "leads.csv"
    path.write_text(HEADER + ROW, encoding="utf-8")
    return str(path)


def test_main_writes_ranked_and_excluded_csv(sample_csv, tmp_path, capsys):
    out_path = tmp_path / "out" / "ranked.csv"
    exit_code = main([sample_csv, "-o", str(out_path)])
    assert exit_code == 0
    assert out_path.exists()
    assert (tmp_path / "out" / "ranked.excluded.csv").exists()

    written = pd.read_csv(out_path)
    assert len(written) == 1
    assert written.iloc[0]["recommendation"] == "Contact Now"

    captured = capsys.readouterr()
    assert "Ranked leads written to" in captured.out


def test_main_quiet_suppresses_summary(sample_csv, tmp_path, capsys):
    out_path = tmp_path / "out2" / "ranked.csv"
    main([sample_csv, "-o", str(out_path), "--quiet"])
    captured = capsys.readouterr()
    assert captured.out == ""


def test_main_missing_file_exits_nonzero(tmp_path, capsys):
    missing = str(tmp_path / "does_not_exist.csv")
    with pytest.raises(SystemExit) as exc_info:
        main([missing])
    assert exc_info.value.code != 0


def test_main_no_input_given_exits_nonzero(monkeypatch, capsys):
    monkeypatch.delenv("LEADS_INPUT_PATH", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code != 0


def test_main_custom_thresholds_affect_output(sample_csv, tmp_path):
    out_path = tmp_path / "strict.csv"
    main([sample_csv, "-o", str(out_path), "--contact-now-threshold", "200", "--nurture-threshold", "150", "--quiet"])
    written = pd.read_csv(out_path)
    assert written.iloc[0]["recommendation"] == "Disqualify"
