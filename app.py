"""Lead Qualification Dashboard.

A Streamlit front-end over `lead_qualifier.pipeline`. Upload a messy lead
export, get it cleaned, deduplicated, scored, and ranked -- with filters,
an audit trail of what was dropped, and CSV downloads.

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import io
import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from lead_qualifier import pipeline, scoring

# ---------------------------------------------------------------------------
# Status palette (fixed, never themed) -- one role per recommendation bucket.
# These are the dataviz "status" colors: good/warning/critical are reserved
# for exactly this kind of state indicator and never reused as generic
# chart series colors elsewhere in the app.
# ---------------------------------------------------------------------------
STATUS_COLORS = {
    scoring.CONTACT_NOW: "#0ca30c",  # good
    scoring.NURTURE: "#fab219",      # warning
    scoring.DISQUALIFY: "#d03b3b",   # critical
}
# Paired with an icon (not color alone) everywhere a recommendation is shown,
# so the meaning still reads for colorblind users / on low-contrast text.
STATUS_ICONS = {
    scoring.CONTACT_NOW: "🟢",
    scoring.NURTURE: "🟡",
    scoring.DISQUALIFY: "🔴",
}
# Fixed display order for the three buckets -- used for the chart, legend,
# and excluded-tab metrics so they always appear in the same funnel order
# rather than whatever order value_counts() happens to return.
BUCKET_ORDER = [scoring.CONTACT_NOW, scoring.NURTURE, scoring.DISQUALIFY]

# Shown automatically if the user hasn't uploaded their own file yet, so the
# dashboard is immediately explorable rather than starting on a blank page.
SAMPLE_CSV_PATH = "Cohort 3 Assessment — Task 1 Leads (messy).csv"

st.set_page_config(
    page_title="Lead Qualification Dashboard",
    page_icon="📋",
    layout="wide",
)

# Light styling for Streamlit's built-in metric cards (st.metric) -- gives
# them a bordered "card" look consistent with the rest of the page instead
# of Streamlit's bare default.
st.markdown(
    """
    <style>
    div[data-testid="stMetric"] {
        background-color: #f9f9f7;
        border: 1px solid rgba(11,11,11,0.10);
        border-radius: 10px;
        padding: 14px 16px 10px 16px;
    }
    div[data-testid="stMetricValue"] { font-size: 1.6rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner="Cleaning, deduplicating, and scoring leads…")
def cached_run(file_bytes: bytes, contact_now_threshold: float, nurture_threshold: float):
    """Run the pipeline and cache the result.

    Streamlit reruns the whole script top-to-bottom on every interaction
    (typing in a filter box, moving a slider, ...), so without caching we'd
    re-clean and re-score all ~500 leads on every keystroke. @st.cache_data
    keys its cache on the function's arguments -- since ``file_bytes`` is the
    *content* of the uploaded file (not a live file handle), the cache
    correctly invalidates only when the file or thresholds actually change.

    Returns plain (DataFrame, DataFrame, dict) rather than the
    PipelineResult dataclass so the cached value is simple, picklable data.
    """
    buffer = io.BytesIO(file_bytes)
    result = pipeline.run_pipeline(
        buffer,
        contact_now_threshold=contact_now_threshold,
        nurture_threshold=nurture_threshold,
    )
    stats = {
        "total_rows_read": result.total_rows_read,
        "junk_rows_dropped": result.junk_rows_dropped,
        "duplicate_rows_dropped": result.duplicate_rows_dropped,
        "qualified_rows": result.qualified_rows,
        "counts_by_recommendation": result.counts_by_recommendation,
    }
    return result.ranked, result.excluded, stats


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    """Encode a DataFrame as UTF-8 CSV bytes for st.download_button."""
    return df.to_csv(index=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Sidebar: data source + scoring thresholds
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("📋 Lead Qualifier")
    st.caption("Upload a lead export, or explore the bundled sample.")

    uploaded = st.file_uploader("Lead export CSV", type=["csv"])

    # Precedence: an uploaded file always wins; otherwise fall back to the
    # bundled sample CSV so the dashboard has something to show immediately;
    # if even that's missing, file_bytes stays None and we show a warning
    # further down instead of crashing.
    if uploaded is not None:
        file_bytes = uploaded.getvalue()
        source_label = uploaded.name
    elif os.path.isfile(SAMPLE_CSV_PATH):
        with open(SAMPLE_CSV_PATH, "rb") as f:
            file_bytes = f.read()
        source_label = f"Sample data ({SAMPLE_CSV_PATH})"
        st.info("No file uploaded -- showing bundled sample data.")
    else:
        file_bytes = None
        source_label = None

    st.divider()
    st.subheader("Scoring thresholds")
    # Contact Now threshold: the score at/above which a lead is top-priority.
    contact_now_threshold = st.slider(
        "Contact Now  (score ≥)", min_value=0, max_value=100,
        value=int(scoring.DEFAULT_CONTACT_NOW_THRESHOLD), step=1,
    )
    # Nurture threshold: must stay below the Contact Now threshold, since a
    # lead can't need less score to "Nurture" than to "Contact Now" -- the
    # slider's own max is clamped to contact_now_threshold - 1 to enforce
    # that ordering directly in the widget rather than validating after.
    nurture_threshold = st.slider(
        "Nurture  (score ≥)", min_value=0, max_value=contact_now_threshold - 1,
        value=min(int(scoring.DEFAULT_NURTURE_THRESHOLD), max(contact_now_threshold - 1, 0)), step=1,
    )
    st.caption(
        f"Score ≥ {contact_now_threshold} → Contact Now · "
        f"{nurture_threshold}–{contact_now_threshold - 1} → Nurture · "
        f"< {nurture_threshold} → Disqualify. Notes matching a hard "
        "disqualifier (spam, job seeker, etc.) always disqualify."
    )

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
st.title("Lead Qualification Dashboard")
st.caption("Clean → deduplicate → score → rank, from a raw CSV export.")

if file_bytes is None:
    st.warning("Upload a CSV to get started -- no bundled sample data was found.")
    st.stop()

try:
    ranked_df, excluded_df, stats = cached_run(file_bytes, float(contact_now_threshold), float(nurture_threshold))
except ValueError as e:
    # run_pipeline() raises ValueError for a structurally invalid CSV (e.g.
    # missing a required column) -- surface that as a friendly error
    # instead of letting Streamlit show a raw traceback.
    st.error(f"Couldn't process **{source_label}**: {e}")
    st.stop()

st.caption(f"Source: **{source_label}**")

# --- Summary metrics --------------------------------------------------------
# Five headline numbers: how much data came in, how much was cleaned away,
# and the size of the top-priority bucket -- the things a user wants to see
# in the first second, before scrolling to any detail.
counts = stats["counts_by_recommendation"]
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Rows read", stats["total_rows_read"])
m2.metric("Junk/blank dropped", stats["junk_rows_dropped"])
m3.metric("Duplicates merged", stats["duplicate_rows_dropped"])
m4.metric("Leads scored", stats["qualified_rows"])
m5.metric("🟢 Contact Now", counts.get(scoring.CONTACT_NOW, 0))

# --- Recommendation breakdown chart -----------------------------------------
# Horizontal bar chart, one bar per bucket, colored with the fixed status
# palette and labeled with its count directly on the bar (never relying on
# color alone to convey the value) -- plus a text legend beside it that
# repeats the same icon+label+count for accessibility/redundancy.
chart_col, legend_col = st.columns([3, 1])
with chart_col:
    y_vals = [counts.get(b, 0) for b in BUCKET_ORDER]
    fig = go.Figure(
        go.Bar(
            x=y_vals,
            y=BUCKET_ORDER,
            orientation="h",
            marker_color=[STATUS_COLORS[b] for b in BUCKET_ORDER],
            text=[str(v) for v in y_vals],
            textposition="outside",
            hovertemplate="%{y}: %{x} leads<extra></extra>",
        )
    )
    fig.update_layout(
        height=220,
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis=dict(title=None, showgrid=True, gridcolor="#e1e0d9", zeroline=False),
        yaxis=dict(title=None, autorange="reversed"),  # Contact Now on top, Disqualify on bottom
        plot_bgcolor="#fcfcfb",
        paper_bgcolor="#fcfcfb",
        showlegend=False,
    )
    # width="stretch" is the default in current Streamlit -- passed
    # explicitly for clarity/forward-compatibility rather than relying on
    # the (deprecated) use_container_width=True.
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
with legend_col:
    st.write("")
    for b in BUCKET_ORDER:
        st.markdown(f"{STATUS_ICONS[b]} **{b}** — {counts.get(b, 0)}")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_ranked, tab_excluded, tab_methodology = st.tabs(
    ["🏆 Ranked Leads", "🧹 Excluded / Duplicates", "📖 Methodology"]
)

with tab_ranked:
    # Filter row: recommendation bucket, free-text search, score range.
    # All three combine with AND (a row must pass every active filter).
    f1, f2, f3 = st.columns([2, 3, 2])
    with f1:
        rec_filter = st.multiselect(
            "Recommendation", BUCKET_ORDER, default=BUCKET_ORDER,
        )
    with f2:
        search = st.text_input("Search (name, company, email, title)", "")
    with f3:
        score_range = st.slider("Score range", 0, 100, (0, 100))

    filtered = ranked_df[ranked_df["recommendation"].isin(rec_filter)]
    filtered = filtered[
        (filtered["score"] >= score_range[0]) & (filtered["score"] <= score_range[1])
    ]
    if search.strip():
        # Case-insensitive substring match across four identifying columns;
        # a row matches if ANY of them contains the search text.
        needle = search.strip().lower()
        haystack = (
            filtered[["name", "company", "email", "title"]]
            .astype(str)
            .apply(lambda col: col.str.lower())
        )
        mask = haystack.apply(lambda col: col.str.contains(needle, na=False)).any(axis=1)
        filtered = filtered[mask]

    st.caption(f"Showing {len(filtered)} of {len(ranked_df)} leads")

    # Prefix the recommendation text with its status icon for the on-screen
    # table only -- CSV downloads (below) keep the plain text value so the
    # exported file stays clean/machine-readable.
    display_df = filtered.copy()
    display_df["recommendation"] = display_df["recommendation"].map(
        lambda r: f"{STATUS_ICONS.get(r, '')} {r}"
    )

    st.dataframe(
        display_df,
        width="stretch",
        hide_index=True,
        # Custom rendering per column: a visual progress bar for the score
        # (faster to scan than a bare number), a checkbox for the boolean
        # email_valid flag, and wide text columns for the free-form
        # "reasons"/"notes" fields so they aren't truncated too aggressively.
        column_config={
            "score": st.column_config.ProgressColumn(
                "Total Score", min_value=0, max_value=100, format="%.0f"
            ),
            "fit_score": st.column_config.NumberColumn("Fit (/40)", format="%.0f"),
            "intent_score": st.column_config.NumberColumn("Intent (/40)", format="%.0f"),
            "engagement_score": st.column_config.NumberColumn("Stage (/20)", format="%.0f"),
            "engagement_stage": st.column_config.TextColumn("Engagement Stage"),
            "email_valid": st.column_config.CheckboxColumn("Email valid"),
            "recommendation": st.column_config.TextColumn("Recommendation"),
            "justification": st.column_config.TextColumn("Justification", width="large"),
            "notes": st.column_config.TextColumn("Notes", width="large"),
            # Explicit NumberColumns so a missing value (NaN, for leads that
            # never stated a headcount/budget) renders as a blank cell
            # rather than Streamlit's default "None" placeholder text.
            "employees": st.column_config.NumberColumn("Employees", format="%d"),
            "monthly_budget": st.column_config.NumberColumn("Monthly budget", format="$%d"),
        },
    )

    # Two download options: just what's currently filtered, or everything --
    # covers both "I want this specific view" and "give me the full dataset".
    dl1, dl2 = st.columns(2)
    dl1.download_button(
        "⬇️ Download filtered leads (CSV)", to_csv_bytes(filtered),
        file_name="ranked_leads_filtered.csv", mime="text/csv",
    )
    dl2.download_button(
        "⬇️ Download all ranked leads (CSV)", to_csv_bytes(ranked_df),
        file_name="ranked_leads.csv", mime="text/csv",
    )

with tab_excluded:
    st.caption(
        "Rows removed before scoring: blank rows, stray header rows, QA/test "
        "placeholders, and duplicate submissions (kept the most complete copy)."
    )
    if excluded_df.empty:
        st.success("Nothing was excluded from this file.")
    else:
        # One metric per exclusion reason (the four reason codes pipeline.py
        # emits), so the mix of "why" is visible before reading the raw table.
        reason_counts = excluded_df["reason"].value_counts()
        rc1, rc2, rc3, rc4 = st.columns(4)
        for col, reason in zip((rc1, rc2, rc3, rc4), [
            "blank_row", "embedded_header_row", "test_placeholder_row", "duplicate_submission",
        ]):
            col.metric(reason.replace("_", " ").title(), int(reason_counts.get(reason, 0)))
        st.dataframe(excluded_df, width="stretch", hide_index=True)
        st.download_button(
            "⬇️ Download excluded rows (CSV)", to_csv_bytes(excluded_df),
            file_name="excluded_rows.csv", mime="text/csv",
        )

with tab_methodology:
    # A short, always-current explanation of the scoring model -- the
    # threshold numbers are f-string-interpolated from the live sidebar
    # sliders, so this text never goes stale relative to what's on screen.
    st.markdown(
        f"""
### Inferred ICP

{scoring.ICP_SUMMARY}

*(Derived empirically from the 85 leads in the sample data whose notes combine
an explicit budget commitment with an urgency phrase -- the strongest-signal
group, independent of any scoring system. Full derivation in README.md.)*

### How leads are scored

Every lead gets a **0–100 score** from three parts, modeled on how an SDR
triages a lead against that ICP:

- **Fit (0–40)** — does this company/contact match the ICP above? ICP
  vertical match (0–20) + company-size fit (0–10) + title seniority (0–10).
- **Intent (0–40)** — do the notes show genuine interest, urgency, or
  budget signals? Notes-phrase signal (0–30) + the stated monthly-budget
  field, also treated as a budget signal (0–10). If notes are blank or too
  vague to read any signal from, Intent is scored conservatively low and
  the justification says so explicitly, rather than guessing.
- **Engagement stage (0/10/20)** — cold contact, warm conversation, or
  sales-ready lead, based on *which* phrases matched (a commitment phrase
  like "budget approved" reads as sales-ready regardless of point total).

**Hard disqualifiers** short-circuit straight to *Disqualify* with score 0
regardless of the above: job seekers, investors/VCs, students/researchers,
press inquiries, competitors, recruiter/vendor pitches, spam, and QA/test
rows. A lead with no name, email, *or* company is disqualified outright —
there's nothing to act on.

**Current thresholds:** score ≥ **{contact_now_threshold}** → 🟢 Contact Now
· **{nurture_threshold}–{contact_now_threshold - 1}** → 🟡 Nurture · below
**{nurture_threshold}** → 🔴 Disqualify. Adjust these in the sidebar — the
table re-scores instantly.

**Before scoring**, rows are cleaned and deduplicated: dates, employee
counts, and budgets are parsed from whatever free-form format they were
entered in; emails are validated; blank rows, stray header rows, and
QA/test placeholders are dropped; and duplicate submissions (matched by a
canonical lead id) are merged down to the most complete, most recent copy.

See `README.md` in the project for the full methodology and the CLI
(`python -m lead_qualifier.cli`) for scripted/unattended runs.
        """
    )
