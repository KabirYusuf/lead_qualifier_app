"""Command-line entry point.

Usage:
    python -m lead_qualifier.cli path/to/leads.csv -o output/ranked_leads.csv

Defaults for input/output paths and score thresholds can also be set via a
``.env`` file (see ``.env.example``); CLI flags always take precedence.
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

from . import scoring
from .pipeline import run_pipeline, summary_lines, write_outputs


def _env_float(name: str, default: float) -> float:
    """Read a float from an environment variable, falling back to
    ``default`` if it's unset or not a valid number (never raises)."""
    val = os.environ.get(name)
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI's argument parser.

    Every option's default is layered: built-in default -> ``.env`` value
    (already loaded into ``os.environ`` by ``main()`` before this runs) ->
    explicit CLI flag, in that order of increasing priority.
    """
    parser = argparse.ArgumentParser(
        prog="lead_qualifier",
        description="Clean, score, and rank a lead export CSV.",
    )
    # Positional and optional -- nargs="?" means it can be omitted from the
    # command line entirely, in which case we fall through to .env / error.
    parser.add_argument(
        "input_csv", nargs="?",
        default=os.environ.get("LEADS_INPUT_PATH"),
        help="Path to the raw lead export CSV. Falls back to LEADS_INPUT_PATH in .env.",
    )
    parser.add_argument(
        "-o", "--output", dest="output_csv",
        default=os.environ.get("LEADS_OUTPUT_PATH", "output/ranked_leads.csv"),
        help="Path to write the ranked leads CSV. Falls back to LEADS_OUTPUT_PATH in .env "
             "(default: output/ranked_leads.csv).",
    )
    parser.add_argument(
        "--excluded-output", dest="excluded_csv", default=None,
        help="Optional path to write an audit CSV of rows dropped as junk/duplicates "
             "(default: <output>.excluded.csv next to the ranked output).",
    )
    parser.add_argument(
        "--contact-now-threshold", type=float,
        default=_env_float("CONTACT_NOW_THRESHOLD", scoring.DEFAULT_CONTACT_NOW_THRESHOLD),
        help=f"Score at/above which a lead is 'Contact Now' (default: {scoring.DEFAULT_CONTACT_NOW_THRESHOLD}).",
    )
    parser.add_argument(
        "--nurture-threshold", type=float,
        default=_env_float("NURTURE_THRESHOLD", scoring.DEFAULT_NURTURE_THRESHOLD),
        help=f"Score at/above which a lead is 'Nurture' rather than 'Disqualify' "
             f"(default: {scoring.DEFAULT_NURTURE_THRESHOLD}).",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress the summary printed to stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code (0 = success).

    ``argv`` is normally ``None`` (argparse then reads real ``sys.argv``);
    tests pass an explicit list instead so they don't depend on how pytest
    itself was invoked.
    """
    load_dotenv()  # populate os.environ from a local .env file, if present
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # parser.error() prints a usage message and raises SystemExit -- these
    # are deliberate hard stops, not exceptions to recover from.
    if not args.input_csv:
        parser.error(
            "No input CSV given. Pass it as an argument or set LEADS_INPUT_PATH in .env."
        )

    if not os.path.isfile(args.input_csv):
        parser.error(f"Input CSV not found: {args.input_csv}")

    # Default the excluded-rows audit path to sit next to the ranked output
    # (e.g. "output/ranked.csv" -> "output/ranked.excluded.csv") unless the
    # caller asked for somewhere else explicitly.
    excluded_csv = args.excluded_csv
    if excluded_csv is None:
        root, _ext = os.path.splitext(args.output_csv)
        excluded_csv = f"{root}.excluded.csv"

    result = run_pipeline(
        args.input_csv,
        contact_now_threshold=args.contact_now_threshold,
        nurture_threshold=args.nurture_threshold,
    )
    write_outputs(result, args.output_csv, excluded_csv)

    if not args.quiet:
        print(f"Ranked leads written to:   {args.output_csv}")
        print(f"Excluded rows audit at:    {excluded_csv}")
        print()
        for line in summary_lines(result):
            print(line)

    return 0


if __name__ == "__main__":
    sys.exit(main())
