"""report.md: the ranked human summary plus the harness SCOPE and
coverage sections (R14, R16; spec "Output contract" and "Harness notes").

Sections: entry-point delta, ranked guard hunks, a SCOPE block listing
new entry points as candidate lines with shape tags guessed from the
guard classes, and a coverage ledger with one row per new entry point
plus adapter gap notes. Ordering is deterministic everywhere.
"""

from pathlib import Path

from ._util import write_text
from .adapters import LOW_CONFIDENCE_ATTR
from .rank import RankedHunk, rank_delta, surface_weight

ENTRY_KINDS = ("route", "rpc", "protocol_id")


def _entry_records(surface: dict) -> dict[str, list]:
    """Delta lists restricted to entry-point kinds (route/rpc/protocol_id)."""
    ranked = rank_delta(surface)

    def kind_of(item: dict) -> str:
        # changed/renamed entries are {before, after} pairs; the after side
        # carries the kind when both differ.
        if "after" in item or "before" in item:
            return _rec_kind(item.get("after") or item.get("before") or {})
        return _rec_kind(item)

    return {
        bucket: [r for r in ranked[bucket] if kind_of(r) in ENTRY_KINDS]
        for bucket in ("added", "removed", "changed", "renamed")
    }


def _rec_kind(rec: dict) -> str:
    return (rec or {}).get("kind", "")


def shape_tags(record: dict, hunks: list[RankedHunk]) -> list[str]:
    """Spec shape tags guessed from guard evidence near the entry point.

    `length-field` when a bounds hunk touches length/size evidence in the
    record's symbol or file; `dispatch=<handled>/<upstream enum>` when a
    dispatch hunk in the same symbol implies an arm count changed (handled
    arms = dispatch hunks observed, upstream enum = arms + 1 placeholder
    wording per spec); `amplify` when hunk evidence loops over an
    attacker-controlled count.
    """
    tags: list[str] = []
    sym = record.get("symbol") or ""
    path = record.get("file") or ""
    nearby = [
        r for r in hunks
        if (sym and r.hunk.symbol == sym) or r.hunk.file == path
    ]
    arms = sum(1 for x in nearby if x.hunk.hunk_class == "dispatch")
    for r in nearby:
        ev = (r.hunk.evidence or "").lower()
        if r.hunk.hunk_class == "bounds" and any(
            w in ev for w in ("len", "size", "count", "length", "capacity")
        ):
            if "length-field" not in tags:
                tags.append("length-field")
        if r.hunk.hunk_class == "dispatch":
            tag = f"dispatch={arms}/{arms + 1} upstream enum"
            if tag not in tags:
                tags.append(tag)
        if any(w in ev for w in ("for ", "while ", "range", "each")) and any(
            w in ev for w in ("peer", "request", "conn", "msg", "packet", "input", "len")
        ):
            if "amplify" not in tags:
                tags.append("amplify")
    return tags


def build_report(
    surface: dict,
    ranked_hunks: list[RankedHunk],
    gap_notes: list[str],
    *,
    repo: str,
    from_ref: str,
    to_ref: str,
    generated_at: str,
) -> str:
    """The report.md document for one tag pair, as text."""
    entries = _entry_records(surface)
    lines: list[str] = []
    lines.append(f"# release surface diff: {repo} {from_ref}..{to_ref}")
    lines.append("")
    lines.append(f"generated: {generated_at}")
    lines.append("")

    lines.append("## Entry-point delta")
    lines.append("")
    for bucket, label in (
        ("added", "Added"),
        ("changed", "Changed"),
        ("renamed", "Renamed"),
        ("removed", "Removed"),
    ):
        recs = entries[bucket]
        lines.append(f"### {label} ({len(recs)})")
        lines.append("")
        if not recs:
            lines.append("- none")
        for item in recs:
            if bucket in ("changed", "renamed"):
                before = item.get("before") or {}
                after = item.get("after") or {}
                weight = surface_weight(_rec_kind(after) or _rec_kind(before))
                lines.append(
                    f"- `{before.get('id','?')}` -> `{after.get('id','?')}`"
                    f" ({after.get('file','?')}:{after.get('line') or 0},"
                    f" weight {weight})"
                )
            else:
                weight = surface_weight(_rec_kind(item))
                low = " (low confidence)" if (item.get("attrs") or {}).get(
                    LOW_CONFIDENCE_ATTR
                ) else ""
                lines.append(
                    f"- `{item.get('id','?')}`"
                    f" ({item.get('file','?')}:{item.get('line') or 0},"
                    f" weight {weight}){low}"
                )
        lines.append("")

    lines.append("## Guard hunks (ranked)")
    lines.append("")
    if not ranked_hunks:
        lines.append("- none")
    for r in ranked_hunks:
        h = r.hunk
        marks = []
        if r.in_delta_symbol:
            marks.append("delta-symbol")
        if r.reachable:
            marks.append("reachable")
        mark = f" [{', '.join(marks)}]" if marks else ""
        lines.append(
            f"- {r.score} | {h.hunk_class} {h.direction} |"
            f" {h.file}:{h.new_start or h.old_start} | {h.symbol or '?'}"
            f" | {h.evidence}{mark}"
        )
    lines.append("")

    lines.append("## SCOPE")
    lines.append("")
    added = entries["added"]
    if not added:
        lines.append("- none")
    for rec in added:
        tags = shape_tags(rec, ranked_hunks)
        tag_str = " ".join(f"[{t}]" for t in tags)
        lines.append(
            f"- {rec.get('id','?')} ({rec.get('file','?')}:{rec.get('line') or 0})"
            f" {tag_str}".rstrip()
        )
    lines.append("")

    lines.append("## Coverage")
    lines.append("")
    if not added:
        lines.append("- none")
    for rec in added:
        lines.append(
            f"- [ ] {rec.get('id','?')}"
            f" ({rec.get('file','?')}:{rec.get('line') or 0}) uncovered"
        )
    if gap_notes:
        lines.append("")
        lines.append("### Gap notes")
        lines.append("")
        for note in gap_notes:
            lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def write_report(text: str, path) -> Path:
    return write_text(path, text)
