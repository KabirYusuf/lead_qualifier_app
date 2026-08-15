"""End-to-end pipeline: raw CSV -> cleaned, deduplicated, scored, ranked CSV.

This is the piece meant to be re-run against future lead exports. It is
deliberately structured as a sequence of small, inspectable steps (read ->
filter junk -> clean -> dedupe -> score -> rank -> write) with an audit
trail of everything that was dropped or merged along the way, so the
marketing team can trust -- and verify -- what the automation did.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from . import cleaning, scoring
from .dedup import dedupe_leads

# The columns every input CSV must have (order doesn't matter -- pandas
# matches by name). Used both to validate the input and to know which
# columns to check when scanning for blank/header-echo rows.
COLUMNS = [
    "lead_id", "created", "name", "email", "company",
    "employees", "website", "title", "source", "monthly_budget", "notes",
]

# Columns written to the ranked-leads output CSV, in display order. Deliberately
# excludes the internal bookkeeping fields (lead_id_canonical, source_row, ...)
# that only matter inside the pipeline -- the exported CSV is meant for people.
OUTPUT_COLUMNS = [
    "lead_id", "created", "name", "email", "email_valid", "company", "website",
    "employees", "title", "source", "monthly_budget", "notes",
    "fit_score", "intent_score", "engagement_score", "score", "engagement_stage",
    "recommendation", "justification",
]

# Columns written to the excluded-rows audit CSV.
EXCLUDED_COLUMNS = ["source_row", "lead_id", "reason", "detail", "notes"]


def load_raw_csv(path) -> pd.DataFrame:
    """Load a lead export as plain strings -- no numeric/date auto-coercion.

    ``path`` may be a filesystem path or any file-like/buffer object pandas
    accepts (e.g. an uploaded file's bytes wrapped in ``io.BytesIO`` -- see
    the Streamlit dashboard in ``app.py``).

    Reading everything as ``str`` (blank cells as ``""``, not ``NaN``) keeps
    every downstream parser dealing with one predictable input shape instead
    of juggling pandas' type inference on top of already-messy data. Encoding
    is pinned to UTF-8 explicitly -- relying on the platform default silently
    mangles non-ASCII characters (em dashes, accented names) on some systems.
    """
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False, encoding="utf-8")
    df.columns = [c.strip() for c in df.columns]
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Input CSV is missing expected column(s): {missing}. "
            f"Found columns: {list(df.columns)}"
        )
    return df


def clean_record(row: dict, source_row: int) -> dict:
    """Turn one raw CSV row (dict of strings) into a normalized record.

    ``source_row`` is the 1-indexed line number in the original CSV
    (including the header line), carried through purely for traceability --
    it's what shows up in the excluded-rows audit and, as a last resort,
    becomes the lead's display id if it has no usable lead_id of its own.

    Only row-level junk (blank/header-echo/test rows) has already been
    filtered out by the time this runs -- see run_pipeline().
    """
    lead_id_info = cleaning.parse_lead_id(row.get("lead_id"))
    email, email_valid = cleaning.clean_email(row.get("email"))
    created_date = cleaning.parse_date(row.get("created"))

    # If the row's own lead_id didn't parse to anything usable, fall back to
    # a synthetic "ROW-<n>" id so every record has *some* stable identifier
    # to display and refer to -- it just won't dedupe against anything
    # (dedupe_leads() only groups records that share a real canonical id).
    display_id = lead_id_info.raw if lead_id_info.looks_valid else f"ROW-{source_row}"

    return {
        "source_row": source_row,
        "lead_id_raw": lead_id_info.raw,
        "lead_id_canonical": lead_id_info.canonical,  # dedup key -- see dedup.dedupe_leads()
        "lead_id_is_explicit_dup": lead_id_info.is_explicit_dup,
        "lead_id": display_id,
        "created_raw": row.get("created"),
        "created_date": created_date,  # a real date() or None; used for dedup tie-breaking
        # Prefer the parsed ISO date for display; if parsing failed, fall
        # back to whatever text was there rather than showing a blank.
        "created": created_date.isoformat() if created_date else (cleaning.clean_text(row.get("created")) or ""),
        "name": cleaning.clean_name(row.get("name")),
        "email": email,
        "email_valid": email_valid,
        "company": cleaning.clean_company(row.get("company")),
        "employees": cleaning.parse_employees(row.get("employees")),
        "website": cleaning.clean_website(row.get("website")),
        "title": cleaning.clean_title(row.get("title")),
        "source": cleaning.clean_source(row.get("source")),
        "monthly_budget": cleaning.parse_budget(row.get("monthly_budget")),
        "notes": cleaning.clean_text(row.get("notes")) or "",
    }


@dataclass
class PipelineResult:
    """Everything run_pipeline() produces: the two output tables plus the
    counts needed for the CLI summary / dashboard metrics, so callers don't
    have to recompute them from the DataFrames."""
    ranked: pd.DataFrame  # one row per lead, sorted by score descending -- the main deliverable
    excluded: pd.DataFrame  # every row dropped (junk or duplicate) and why -- the audit trail
    total_rows_read: int = 0  # raw row count in the input CSV (excluding the header)
    junk_rows_dropped: int = 0  # blank / embedded-header / QA-test rows removed before scoring
    duplicate_rows_dropped: int = 0  # duplicate submissions merged away
    qualified_rows: int = 0  # len(ranked) -- rows that made it to scoring
    counts_by_recommendation: dict = field(default_factory=dict)  # e.g. {"Contact Now": 113, ...}


def run_pipeline(
    input_path: str,
    contact_now_threshold: float = scoring.DEFAULT_CONTACT_NOW_THRESHOLD,
    nurture_threshold: float = scoring.DEFAULT_NURTURE_THRESHOLD,
) -> PipelineResult:
    """Run the full pipeline in memory and return ranked + excluded frames.

    Six stages, in order: (1) load the CSV as raw strings, (2) drop
    row-level junk (blank/header-echo/QA rows), (3) clean every remaining
    field into typed values, (4) deduplicate by canonical lead id, (5) score
    and bucket each surviving lead, (6) sort by score and package up the
    results. Nothing is written to disk here -- see write_outputs().
    """
    df = load_raw_csv(input_path)
    total_rows_read = len(df)

    excluded_rows: list[dict] = []
    records: list[dict] = []

    # --- Stage 2 & 3: filter junk rows, clean everything else -------------
    for i, row in enumerate(df.to_dict("records")):
        source_row = i + 2  # +1 for 0-index, +1 for the header line
        if cleaning.is_blank_row(row, COLUMNS):
            excluded_rows.append({
                "source_row": source_row, "lead_id": row.get("lead_id") or "",
                "reason": "blank_row", "detail": "Row has no data in any column.",
                "notes": row.get("notes") or "",
            })
            continue
        if cleaning.is_header_echo_row(row, COLUMNS):
            excluded_rows.append({
                "source_row": source_row, "lead_id": row.get("lead_id") or "",
                "reason": "embedded_header_row", "detail": "A stray header row leaked into the data.",
                "notes": row.get("notes") or "",
            })
            continue
        if cleaning.is_garbage_test_row(row):
            excluded_rows.append({
                "source_row": source_row, "lead_id": row.get("lead_id") or "",
                "reason": "test_placeholder_row", "detail": "QA/placeholder row, not a real lead.",
                "notes": row.get("notes") or "",
            })
            continue
        records.append(clean_record(row, source_row))

    junk_rows_dropped = len(excluded_rows)

    # --- Stage 4: deduplicate ----------------------------------------------
    dedup_result = dedupe_leads(records)
    for dropped in dedup_result.dropped:
        excluded_rows.append({
            "source_row": dropped["source_row"],
            "lead_id": dropped["lead_id"],
            "reason": "duplicate_submission",
            "detail": f"Duplicate of {dropped['duplicate_of']}",
            "notes": dropped["notes"],
        })
    duplicate_rows_dropped = len(dedup_result.dropped)

    # --- Stage 5: score every surviving lead --------------------------------
    ranked_rows: list[dict] = []
    for rec in dedup_result.kept:
        result = scoring.qualify_lead(
            notes=rec["notes"],
            title=rec["title"],
            employees=rec["employees"],
            budget=rec["monthly_budget"],
            has_name=rec["name"] is not None,
            has_email=rec["email"] is not None,
            has_company=rec["company"] is not None,
            contact_now_threshold=contact_now_threshold,
            nurture_threshold=nurture_threshold,
        )
        ranked_rows.append({
            "lead_id": rec["lead_id"],
            "created": rec["created"],
            "name": rec["name"] or "",
            "email": rec["email"] or "",
            "email_valid": rec["email_valid"],
            "company": rec["company"] or "",
            "website": rec["website"] or "",
            # Leave missing employees/budget as None (-> NaN), not "" -- these
            # are numeric columns, and mixing floats with empty strings makes
            # pandas store them as "object" dtype, which pyarrow (used by
            # Streamlit/st.dataframe to render tables) can fail to convert
            # ("Could not convert '' with type str: tried to convert to
            # double"). None keeps the column purely numeric; it still shows
            # as a blank cell in the CSV output and in the dashboard table.
            "employees": rec["employees"],
            "title": rec["title"] or "",
            "source": rec["source"] or "",
            "monthly_budget": rec["monthly_budget"],
            "notes": rec["notes"],
            "fit_score": round(result.fit_score, 1),
            "intent_score": round(result.intent_score, 1),
            "engagement_score": round(result.stage_score, 1),
            "score": round(result.score, 1),
            "engagement_stage": result.engagement_stage,
            "recommendation": result.recommendation,
            "justification": result.justification,
            # Internal-only sort key, not part of OUTPUT_COLUMNS so it never
            # appears in the exported CSV / dashboard table -- see below.
            "_raw_score": result.raw_score,
        })

    # --- Stage 6: rank and package results ---------------------------------
    # Build with the extra _raw_score column present so it's available to
    # sort on, then drop it once sorting is done.
    ranked_df = pd.DataFrame(ranked_rows, columns=OUTPUT_COLUMNS + ["_raw_score"])
    if not ranked_df.empty:
        # Rank by the displayed (clipped) score first -- that's what
        # actually determines the Contact Now / Nurture / Disqualify
        # bucket. Many leads clip to the same displayed score (e.g. 100),
        # so _raw_score (pre-clip, unbounded) breaks those ties by genuine
        # strength instead of falling straight to an arbitrary lead_id
        # ordering. lead_id remains the final, fully-deterministic tiebreak
        # for the rare case even raw_score matches exactly.
        ranked_df = ranked_df.sort_values(
            by=["score", "_raw_score", "lead_id"], ascending=[False, False, True]
        ).reset_index(drop=True)
    # Drop the internal sort key unconditionally -- it must never appear in
    # the output, empty-DataFrame case included (an empty df skips the sort
    # above entirely, but still carries the column until this runs).
    ranked_df = ranked_df.drop(columns=["_raw_score"])

    excluded_df = pd.DataFrame(excluded_rows, columns=EXCLUDED_COLUMNS)

    counts_by_recommendation = (
        ranked_df["recommendation"].value_counts().to_dict() if not ranked_df.empty else {}
    )

    return PipelineResult(
        ranked=ranked_df,
        excluded=excluded_df,
        total_rows_read=total_rows_read,
        junk_rows_dropped=junk_rows_dropped,
        duplicate_rows_dropped=duplicate_rows_dropped,
        qualified_rows=len(ranked_df),
        counts_by_recommendation=counts_by_recommendation,
    )


def write_outputs(result: PipelineResult, output_path: str, excluded_path: Optional[str] = None) -> None:
    """Write the ranked-leads CSV (and, optionally, the excluded-rows audit
    CSV) to disk, creating parent directories as needed. Pure I/O -- all the
    actual work already happened in run_pipeline()."""
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    result.ranked.to_csv(output_path, index=False)

    if excluded_path:
        excl_dir = os.path.dirname(excluded_path)
        if excl_dir:
            os.makedirs(excl_dir, exist_ok=True)
        result.excluded.to_csv(excluded_path, index=False)


def summary_lines(result: PipelineResult) -> list[str]:
    """Format a PipelineResult as human-readable lines for the CLI to print."""
    lines = [
        f"Rows read:              {result.total_rows_read}",
        f"Junk rows dropped:      {result.junk_rows_dropped}",
        f"Duplicate rows dropped: {result.duplicate_rows_dropped}",
        f"Leads scored & ranked:  {result.qualified_rows}",
        "",
        "Recommendation breakdown:",
    ]
    # Iterate the fixed bucket order (not counts_by_recommendation's natural
    # order) so Contact Now / Nurture / Disqualify always print in the same,
    # predictable sequence -- and so a bucket with zero leads still shows.
    for label in (scoring.CONTACT_NOW, scoring.NURTURE, scoring.DISQUALIFY):
        lines.append(f"  {label:<12} {result.counts_by_recommendation.get(label, 0)}")
    return lines
