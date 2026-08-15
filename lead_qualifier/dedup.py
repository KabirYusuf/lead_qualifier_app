"""Duplicate lead detection and resolution.

Leads are deduplicated on their canonical lead id (digits extracted from the
``lead_id`` column -- see :func:`lead_qualifier.cleaning.parse_lead_id`), so
``"L-1313"``, ``"1313"`` and ``"L-1313-dup"`` are all recognized as the same
underlying lead even though the raw id text differs.

When a group of duplicates is found, one record is kept ("primary") and the
rest are dropped, preferring in order:

1. A record whose notes are **not** explicitly marked ``"(duplicate
   submission)"`` (the export itself already tells us which copy is the
   redundant one).
2. The more complete record (more populated fields).
3. The more recently created record.
4. Original row order, as a stable tiebreaker.

Dropped rows are never silently discarded -- they're returned alongside the
kept records so the pipeline can report exactly what was merged and why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

_DEFAULT_COMPLETENESS_FIELDS = (
    "name", "email", "company", "employees", "website", "title", "source", "monthly_budget",
)


def _completeness(record: dict, fields: tuple[str, ...]) -> int:
    """Count how many of the given fields are actually populated.

    Used to prefer the more informative record when two duplicates aren't
    otherwise distinguishable by the "(duplicate submission)" marker.
    """
    return sum(1 for f in fields if record.get(f) not in (None, ""))


def _has_dup_marker(record: dict) -> bool:
    """True if the lead's notes were explicitly tagged as a repeat submission."""
    notes = (record.get("notes") or "")
    return "(duplicate submission)" in notes.lower()


@dataclass
class DedupResult:
    """Output of dedupe_leads(): the surviving records plus an audit trail
    of what was dropped and why (so nothing disappears silently)."""
    kept: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)  # each carries a 'duplicate_of' key


def dedupe_leads(
    records: list[dict],
    key_field: str = "lead_id_canonical",
    completeness_fields: tuple[str, ...] = _DEFAULT_COMPLETENESS_FIELDS,
) -> DedupResult:
    """Group records by ``key_field`` and keep one representative per group.

    Records with no usable key (``None``/empty) are never grouped together
    -- each is treated as unique, since we have no reliable way to tell them
    apart.
    """
    # Bucket every record's original index by its dedup key. Records with no
    # key (empty/None canonical id) are simply never added to any bucket, so
    # they can never collide with anything -- see the docstring above.
    groups: dict[str, list[int]] = {}
    for idx, rec in enumerate(records):
        key = rec.get(key_field)
        if not key:
            continue
        groups.setdefault(key, []).append(idx)

    drop_indices: set[int] = set()
    duplicate_of: dict[int, str] = {}

    for idxs in groups.values():
        if len(idxs) <= 1:
            continue  # no duplicates in this group -- nothing to resolve

        def sort_key(i: int) -> tuple:
            rec = records[i]
            created = rec.get("created_date")
            created_ordinal = created.toordinal() if isinstance(created, date) else -1
            return (
                _has_dup_marker(rec),  # False (not marked) sorts first
                -_completeness(rec, completeness_fields),  # more complete sorts first
                -created_ordinal,  # more recent sorts first
                i,  # stable: earliest row wins remaining ties
            )

        # Sorting by sort_key puts the best candidate to keep first; every
        # other record in the group is a duplicate of it.
        ordered = sorted(idxs, key=sort_key)
        primary_idx = ordered[0]
        primary_label = records[primary_idx].get("lead_id_raw") or records[primary_idx].get(key_field)
        for i in ordered[1:]:
            drop_indices.add(i)
            duplicate_of[i] = primary_label

    # Preserve original relative order for whatever's kept.
    kept = [rec for i, rec in enumerate(records) if i not in drop_indices]

    # Tag every dropped record with who it duplicated, for the audit trail
    # (the pipeline surfaces this in the excluded-rows report).
    dropped = []
    for i in sorted(drop_indices):
        rec = dict(records[i])
        rec["duplicate_of"] = duplicate_of[i]
        dropped.append(rec)

    return DedupResult(kept=kept, dropped=dropped)
