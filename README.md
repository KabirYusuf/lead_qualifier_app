# Lead Qualification System

An automated lead qualification system for a marketing agency's inbound
leads. It takes a raw, messy CSV export (bad dates, inconsistent budget
formats, obfuscated emails, duplicate submissions, spam, test rows — the
usual reality of a lead form) and turns it into a **cleaned, deduplicated,
scored, and ranked list** with a clear recommendation per lead:

- 🟢 **Contact Now** — strong intent + good fit, prioritize immediately
- 🟡 **Nurture** — real potential, not ready yet (no budget locked, needs
  internal buy-in, early-stage, etc.)
- 🔴 **Disqualify** — not a sales opportunity (spam, job seeker, student,
  investor, competitor, no usable contact info, or just a poor score)

It ships as **one reusable engine with two front ends**: a command-line
tool for scripted/unattended runs, and a Streamlit dashboard for
interactively uploading a file and exploring the results. Both call the
exact same pipeline code, so results are always identical between them.

```
koya_assesment/
├── lead_qualifier/          # the reusable engine (import this in your own scripts too)
│   ├── cleaning.py          # field parsers: dates, employees, budget, email, website, ids
│   ├── scoring.py           # intent (notes) + fit (firmographics) scoring, buckets
│   ├── dedup.py             # duplicate-submission detection & resolution
│   ├── pipeline.py          # orchestrates: load -> filter junk -> clean -> dedupe -> score -> rank
│   └── cli.py                # command-line entry point
├── app.py                   # Streamlit dashboard (upload a CSV, explore, download)
├── tests/                   # pytest suite -- one file per module above, plus test_cli.py
├── requirements.txt         # pinned dependencies (runtime + dev/test)
├── .env.example             # optional configuration (copy to .env)
├── .streamlit/config.toml   # dashboard theme
├── pytest.ini
└── Cohort 3 Assessment — Task 1 Leads (messy).csv   # the sample/assessment dataset
```

---

## 1. Setup

**Prerequisite:** Python 3.11+ (developed and tested on 3.12).

Everything runs inside a **virtual environment** so dependencies stay local
to this project and never touch your global Python install.

```bash
# 1. Create the virtual environment (once)
python -m venv .venv

# 2. Activate it (every new terminal session)
#    Windows (cmd.exe):
.venv\Scripts\activate.bat
#    Windows (PowerShell):
.venv\Scripts\Activate.ps1
#    macOS / Linux:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
```

`requirements.txt` installs everything needed for both running the app and
running the tests (pandas, python-dateutil, python-dotenv, streamlit,
plotly, pytest, pytest-cov) — one file, one install command.

**Optional configuration:** copy `.env.example` to `.env` and adjust values
if you want different defaults (input/output paths, score thresholds) than
the built-in ones:

```bash
cp .env.example .env      # macOS/Linux
copy .env.example .env    # Windows
```

`.env` is entirely optional — the CLI works fine with just its default
arguments, and the dashboard doesn't read it at all (it's configured
through its own UI widgets instead).

---

## 2. Input format (both CLI and dashboard)

Both front ends expect the **same thing**: a CSV file with (at least) these
11 columns, in any order, with a header row:

| Column | Meaning | Accepted messy formats |
|---|---|---|
| `lead_id` | Unique-ish identifier | `L-1369`, `1369`, `L-1369-dup`, or blank |
| `created` | Date the lead came in | `06/28/2024`, `2024-06-08`, `28-06-2024`, `Jun 7 2024`, `6/1/24` |
| `name` | Contact's name | any text, or blank |
| `email` | Contact's email | `x@y.com`, `x[at]y.com`, broken/missing |
| `company` | Company name | any text, or blank |
| `employees` | Headcount | `20`, `35-55`, `~43`, `70+`, or blank |
| `website` | Company website | `y.com`, `www.y.com`, `http://y.com`, or blank |
| `title` | Contact's job title | `CEO`, `Head of Ops`, `Student`, or blank |
| `source` | How the lead came in | `webform`, `linkedin`, `referral`, `cold reply`, `event` |
| `monthly_budget` | Stated budget | `$6k/mo`, `5,000/mo`, `$6-8k`, `TBD`, `0`, or blank |
| `notes` | Free-text notes | anything — this is what intent scoring reads |

You don't need to clean the file first — that's the entire point of the
tool. Blank rows, a stray header row that leaked into the data, QA/test
placeholder rows, and duplicate submissions are all detected and handled
automatically (see [Methodology](#4-methodology) below). If a required
column is missing entirely, both the CLI and the dashboard will report a
clear error naming the missing column(s) rather than failing silently.

---

## 3. Running it

### Option A — Command line (scripted / unattended runs)

Use this for automation: a cron job, a CI step, or just "give me the file
fast without opening a browser."

```bash
python -m lead_qualifier.cli "Cohort 3 Assessment — Task 1 Leads (messy).csv" -o output/ranked_leads.csv
```

**Input:** the path to your CSV, as the first argument (or set
`LEADS_INPUT_PATH` in `.env` and omit it).

**Output:** two files are written, plus a summary printed to the terminal.

- `output/ranked_leads.csv` — the ranked leads (see column reference below)
- `output/ranked_leads.excluded.csv` — every row that was dropped before
  scoring (blank/junk/duplicate), with a reason, for audit purposes

Console output looks like this:

```
Ranked leads written to:   output/ranked_leads.csv
Excluded rows audit at:    output/ranked_leads.excluded.csv

Rows read:              520
Junk rows dropped:      4
Duplicate rows dropped: 12
Leads scored & ranked:  504

Recommendation breakdown:
  Contact Now  104
  Nurture      78
  Disqualify   322
```

**All CLI options:**

```bash
python -m lead_qualifier.cli --help
```

| Flag | Default | Purpose |
|---|---|---|
| `input_csv` (positional) | `LEADS_INPUT_PATH` from `.env` | Path to the raw lead export |
| `-o, --output` | `output/ranked_leads.csv` | Where to write the ranked CSV |
| `--excluded-output` | `<output>.excluded.csv` | Where to write the audit CSV |
| `--contact-now-threshold` | `65` | Score at/above which a lead is "Contact Now" |
| `--nurture-threshold` | `40` | Score at/above which a lead is "Nurture" (below → "Disqualify") |
| `--quiet` | off | Suppress the summary printed to stdout |

**Example ranked-leads output (top row from the real dataset):**

| lead_id | name | company | fit_score | intent_score | engagement_score | score | engagement_stage | recommendation | justification |
|---|---|---|---|---|---|---|---|---|---|
| 1143 | Amaka | BrightEngine | 40.0 | 40.0 | 20.0 | 100.0 | Sales-Ready | Contact Now | Agency icp fit with decision-maker authority; budget approved and urgency to start; sales-ready -- Contact Now. |

Every row also carries `email_valid` (True/False), `website`, `source`,
`employees`, `monthly_budget`, and the original cleaned `notes` text — see
the full column list in [Output columns](#output-columns) below.

### Option B — Dashboard (interactive)

Use this to actually *look at* the data: upload a file, filter/search,
adjust thresholds live, and download what you need.

```bash
streamlit run app.py
```

This starts a local web server and opens the dashboard in your browser
(typically `http://localhost:8501`).

**Input:** use the **file uploader** in the left sidebar to upload your own
CSV (same 11-column format as above, up to 200MB). If you don't upload
anything, the dashboard automatically shows the bundled sample dataset
(`Cohort 3 Assessment — Task 1 Leads (messy).csv`) so there's always
something to explore.

**What you'll see:**

1. **Summary metrics** across the top: rows read, junk/blank rows dropped,
   duplicates merged, total leads scored, and the Contact Now count.
2. **A horizontal bar chart** breaking down Contact Now / Nurture /
   Disqualify counts, color-coded (green/amber/red) with a text legend
   beside it.
3. **Scoring threshold sliders** in the sidebar — drag "Contact Now" or
   "Nurture" and the entire dashboard (metrics, chart, table) re-scores and
   re-renders instantly, no re-upload needed.
4. **Three tabs:**
   - **🏆 Ranked Leads** — the full sortable table (score shown as a
     progress bar), with filters for recommendation bucket, a free-text
     search across name/company/email/title, and a score-range slider.
     Two download buttons: the currently filtered view, or the full ranked
     list.
   - **🧹 Excluded / Duplicates** — every row dropped before scoring, with
     a metric card per reason (blank row / embedded header / test
     placeholder / duplicate submission) and a downloadable audit table.
   - **📖 Methodology** — a live explanation of the scoring model,
     including your current threshold values.

**Output:** everything in the dashboard is viewable on-screen and
downloadable as CSV via the download buttons — no files are written to
disk automatically the way the CLI does; you choose what to export and
when.

---

## 4. Running tests

```bash
pytest
```

Runs the full suite (139 tests) covering every module: field parsers, note
classification, firmographic scoring, deduplication, the end-to-end
pipeline, and the CLI itself — including the real edge cases found in the
dataset (ambiguous date formats, budget/employee ranges, obfuscated and
broken emails, an embedded stray header row, QA/test placeholder rows,
explicit and implicit duplicate submissions, hard-disqualify note phrases,
and leads with missing contact info).

With a coverage report:

```bash
pytest --cov=lead_qualifier --cov-report=term-missing
```

(97%+ coverage on `lead_qualifier/`; the dashboard itself, `app.py`, is
verified by manually exercising it rather than unit tests, since it's a
thin UI layer over the already-tested pipeline.)

---

## 5. Methodology

### Cleaning

Every field is parsed defensively — messy input never crashes the
pipeline, it just yields "unknown" (`None`):

| Field | Handles |
|---|---|
| `created` | `MM/DD/YYYY`, `M/D/YY`, `YYYY-MM-DD`, `DD-MM-YYYY`, `Mon D YYYY` (see date-format assumption below) |
| `employees` | plain ints, ranges (`"35-55"` → midpoint), `"~43"`, `"70+"` |
| `monthly_budget` | `$`, commas, `k` shorthand, `/mo`, ranges (`"$6-8k"` → midpoint), `"TBD"`/`"depends"` → unknown (not zero) |
| `email` | de-obfuscates `name[at]domain.com`, validates format, keeps invalid ones for manual review instead of discarding |
| `website` | strips scheme/`www.`/trailing slash |
| `lead_id` | canonicalizes `"L-1369"` and `"1369"` to the same id, for dedup |

**Date ambiguity assumption:** hyphenated dates are treated day-first
(`DD-MM-YYYY`), slash-separated dates month-first (`MM/DD/YYYY`) — inferred
from the unambiguous rows in this dataset (e.g. `23-06-2024`, `06/28/2024`).
This is a per-dataset convention, not a universal rule; if a future export
uses the opposite convention, change the `dayfirst` heuristic in
`cleaning.parse_date`.

**Row-level junk is dropped before scoring**, with every drop logged to the
excluded-rows audit: fully blank rows, a stray embedded header row (e.g. a
literal `"header,lead_id,name,..."` data row), and QA/placeholder rows
(`TESTROW`, repeated nonsense values, notes containing "please ignore").

### Deduplication

Leads are grouped by canonical `lead_id` (so `"L-1313"` and `"L-1313-dup"`
merge). Within a duplicate group, one record is kept, preferring: not
marked `"(duplicate submission)"` → more complete → more recently created →
original row order. Everything dropped is logged with what it was a
duplicate of.

### Inferred ICP

Rather than assume an ICP (ideal customer profile) up front, it was derived
empirically: pull every lead whose notes combine an explicit budget
commitment ("budget approved" / "budgeted, serious") with an urgency phrase
("wants to start ASAP", "this is my priority to solve", ...) — 85 leads in
the sample data, the strongest-signal group independent of any scoring
system — and look at what they have in common.

> **Marketing/growth agencies** (any sub-vertical: SEO, social, cold email,
> PPC, demand gen, etc.), roughly **4–80 employees** (median ~44), with a
> **monthly budget of $4,000–$18,000** (median ~$8,500), run by **ops/growth
> leadership or an owner**, whose pain is **repetitive manual work** across
> lead handling, CRM data entry, reporting, or client comms — spanning
> African and other global English-speaking markets.

Full derivation (vertical breakdown, size/budget percentiles, pain-point
frequency, title/geography distribution) is in `scoring.py`'s module
docstring and the `ICP_SUMMARY` constant, which the dashboard's Methodology
tab also displays live.

### Scoring (0–100 per lead), modeled on an SDR's triage

Every lead gets three independently-scored components, mirroring how an
experienced SDR assesses a lead against the ICP above:

- **Fit (0–40 pts)** — does this company/contact match the ICP? ICP
  vertical match (0–20: agency mention = full, adjacent business like
  "SaaS company"/"ecom brand" = half, neither = 0) + company-size fit
  (0–10) + title seniority (0–10: decision-maker > influencer > other >
  low-authority).
- **Intent (0–40 pts)** — do the notes show genuine interest, urgency, or
  budget signals? A weighted phrase lexicon on the notes text (0–30:
  positive for `"budget approved"`, urgency phrases, confirmed authority;
  negative for `"no real budget yet"`, `"price sensitive"`, etc. — see
  `scoring.INTENT_SIGNAL_PATTERNS`) **plus** the stated monthly-budget
  field, also counted as a budget signal (0–10). If notes are blank or
  too vague to read any signal from, Intent is scored conservatively low
  rather than guessed, and the justification says so explicitly.
- **Engagement stage (0/10/20 pts)** — Cold / Warm / Sales-Ready, derived
  from *which* phrases matched (a commitment phrase like `"budget
  approved"` reads as sales-ready regardless of its point total), not from
  the score.

**Hard disqualifiers** (job seekers, investors/VCs, students/researchers,
press, competitors, recruiter/vendor pitches, spam, QA rows, or a lead with
no name/email/company at all) short-circuit straight to **Disqualify** with
score 0 — no amount of budget rescues a journalist asking for a quote.

**Buckets** (tunable via CLI flags / `.env` / dashboard sliders): score ≥
**65** → Contact Now · **40–64** → Nurture · below **40** → Disqualify.

Every scored lead carries a one-line, human-readable `justification`
explaining *why* it landed where it did — meant to be spot-checked by a
person, not blindly trusted.

### Output columns

The ranked CSV / dashboard table has these columns:

`lead_id, created, name, email, email_valid, company, website, employees, title, source, monthly_budget, notes, fit_score, intent_score, engagement_score, score, engagement_stage, recommendation, justification`

- `fit_score` / `intent_score` / `engagement_score` — the three components
  above, out of 40 / 40 / 20 respectively.
- `score` — `fit_score + intent_score + engagement_score`, 0–100.
- `engagement_stage` — `Cold` / `Warm` / `Sales-Ready`.
- `recommendation` — `Contact Now` / `Nurture` / `Disqualify`.
- `justification` — one concise sentence explaining the verdict.
- `email_valid` — `True`/`False`, whether the (cleaned) email looks
  structurally valid — invalid emails are kept, not discarded, for manual
  follow-up.

The excluded-rows CSV has: `source_row, lead_id, reason, detail, notes` —
`source_row` is the original CSV line number, `reason` is one of
`blank_row`, `embedded_header_row`, `test_placeholder_row`,
`duplicate_submission`.

## 6. Design choices worth knowing about

- **Lexicon-based, not template-matching.** Notes in this export are
  template-like, but the scorer matches individual phrases/patterns rather
  than whole templates, so it keeps working on future exports phrased
  slightly differently — at the cost of needing occasional lexicon tuning
  if a new phrase should carry weight it currently doesn't.
- **Missing data is scored conservatively low, never guessed or inflated.**
  A blank notes field, or a structured budget/employee count that was never
  filled in, contributes a small but nonzero score — not the flattering
  "neutral middle" a naive default would give it, and not zero either. The
  `justification` says explicitly when this happened (e.g. "notes are
  blank/too vague to assess intent").
- **Everything dropped is logged**, never silently discarded — blank rows,
  junk rows, and merged duplicates all show up in the excluded-rows audit
  CSV so the process is checkable, not a black box.
- **One engine, two front ends.** `app.py` and `lead_qualifier/cli.py` both
  call `lead_qualifier.pipeline.run_pipeline()` directly — there is no
  duplicated cleaning/scoring logic between them, so they can never
  disagree on a result.
