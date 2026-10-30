"""Diff two extracted surfaces into the spec's surface.json shape.

Records match on (kind, id), with residual same-key collisions disambiguated
by the (file, symbol) ordinal that records.matching_keys assigns (KTD5).
Matched pairs are `changed` when the handler symbol, the middleware-chain
attr (attrs.auth_hint), the file, or the symbol-table body hash differs;
changed entries carry a `body_diff` extracted from symbol-table ranges
(KTD6, KTD7). Leftover removed/added records pair into `renamed` only when
exactly one of each shares a body hash over at least RENAME_MIN_BODY_LINES
non-blank lines (KTD6).
"""

import difflib
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import adapters as _adapters
from ._util import canonical_json, utc_now, write_text
from .records import Record, SymbolSpan, matching_keys, record_sort_key

# KTD6: bodies shorter than this are too generic to prove a rename.
RENAME_MIN_BODY_LINES = 8

# Attr stamped on emitted delta records when either side was extracted
# incompletely (missing/degraded tool, KTD3); surfaced in the report (U7).
LOW_CONFIDENCE_ATTR = _adapters.LOW_CONFIDENCE_ATTR

# Middleware-chain attr whose drift marks a matched record changed (R12).
AUTH_HINT_ATTR = "auth_hint"


@dataclass
class Snapshot:
    """One side's extraction: records, the symbol-table sidecar, file access.

    read_file maps a repo-relative path to file text (dict.get works) and is
    only consulted for records the symbol table covers; records without a
    span get no body hash, no body_diff, and no rename match. low_confidence
    marks a degraded side: every delta computed against it is emitted with
    attrs["low_confidence"] = true.
    """

    records: list[Record]
    symbols: list[SymbolSpan] = field(default_factory=list)
    read_file: Callable[[str], "str | None"] | None = None
    low_confidence: bool = False


# ---- body extraction, normalization, hashing (KTD6, KTD7) ----


def _span_index(symbols: list[SymbolSpan]) -> dict[tuple[str, str], list[SymbolSpan]]:
    index: dict[tuple[str, str], list[SymbolSpan]] = {}
    for span in sorted(symbols, key=lambda s: (s.file, s.symbol, s.start_line, s.end_line)):
        index.setdefault((span.file, span.symbol), []).append(span)
    return index


def _body_text(
    snapshot: Snapshot,
    index: dict[tuple[str, str], list[SymbolSpan]],
    file_text: dict[str, "str | None"],
    record: Record,
) -> "str | None":
    """Symbol-table body text for a record, or None when unavailable."""
    if snapshot.read_file is None:
        return None
    candidates = index.get((record.file, record.symbol))
    if not candidates:
        return None
    span = next(
        (s for s in candidates if s.start_line <= record.line <= s.end_line),
        candidates[0],
    )
    if span.file not in file_text:
        file_text[span.file] = snapshot.read_file(span.file)
    text = file_text[span.file]
    if text is None:
        return None
    lines = text.splitlines()
    return "\n".join(lines[span.start_line - 1 : span.end_line])


def _bodies(
    snapshot: Snapshot,
) -> list["str | None"]:
    index = _span_index(snapshot.symbols)
    file_text: dict[str, "str | None"] = {}
    return [_body_text(snapshot, index, file_text, rec) for rec in snapshot.records]


def _normalize_body(body: str) -> str:
    """Collapse whitespace so whitespace-only drift does not move the hash."""
    return "\n".join(
        " ".join(line.split()) for line in body.splitlines() if line.split()
    )


def _body_hash(body: str) -> str:
    return hashlib.sha256(_normalize_body(body).encode("utf-8")).hexdigest()


def _non_blank_lines(body: str) -> int:
    return sum(1 for line in body.splitlines() if line.strip())


# ---- changed detection and body_diff (R12) ----


def _is_changed(before: Record, after: Record,
                before_body: "str | None", after_body: "str | None") -> bool:
    if before.symbol != after.symbol:
        return True
    if before.file != after.file:
        return True
    if before.attrs.get(AUTH_HINT_ATTR) != after.attrs.get(AUTH_HINT_ATTR):
        return True
    # Body drift counts only when both sides carry a symbol-table body; a
    # missing span means "no body to compare", not "body removed".
    if (
        before_body is not None
        and after_body is not None
        and _body_hash(before_body) != _body_hash(after_body)
    ):
        return True
    return False


def _unified_diff(before: Record, before_body: str,
                  after: Record, after_body: str) -> str:
    lines = difflib.unified_diff(
        before_body.splitlines(),
        after_body.splitlines(),
        fromfile=f"a/{before.file}",
        tofile=f"b/{after.file}",
        lineterm="",
    )
    return "\n".join(lines)


# ---- rename detection (KTD6) ----


def _rename_candidates(
    records: list[Record],
    indexes: list[int],
    bodies: list["str | None"],
) -> dict[tuple[str, str], list[int]]:
    """(kind, body_hash) -> leftover indexes eligible for rename pairing."""
    groups: dict[tuple[str, str], list[int]] = {}
    for i in indexes:
        body = bodies[i]
        if body is None or _non_blank_lines(body) < RENAME_MIN_BODY_LINES:
            continue
        groups.setdefault((records[i].kind, _body_hash(body)), []).append(i)
    return groups


def _detect_renames(
    before: Snapshot, removed: list[int], before_bodies: list["str | None"],
    after: Snapshot, added: list[int], after_bodies: list["str | None"],
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Unique same-kind body-hash pairs are renames; ties stay add+remove."""
    removed_groups = _rename_candidates(before.records, removed, before_bodies)
    added_groups = _rename_candidates(after.records, added, after_bodies)
    pairs: list[tuple[int, int]] = []
    for group_key, removed_idx in removed_groups.items():
        added_idx = added_groups.get(group_key)
        if added_idx is None:
            continue
        if len(removed_idx) == 1 and len(added_idx) == 1:
            pairs.append((removed_idx[0], added_idx[0]))
    renamed_removed = {i for i, _ in pairs}
    renamed_added = {j for _, j in pairs}
    still_removed = [i for i in removed if i not in renamed_removed]
    still_added = [j for j in added if j not in renamed_added]
    return pairs, still_removed, still_added


# ---- surface.json emission (R14, R16) ----


def _record_dict(record: Record, low_confidence: bool) -> dict:
    data = record.to_dict()
    if low_confidence:
        data["attrs"] = {**data["attrs"], LOW_CONFIDENCE_ATTR: True}
    return data


def diff_snapshots(
    before: Snapshot,
    after: Snapshot,
    *,
    repo: str,
    from_ref: str,
    to_ref: str,
    generated_at: "str | None" = None,
) -> dict:
    """Diff two extracted snapshots into the spec's surface.json document.

    `generated_at` is the one intentionally non-deterministic field (R16);
    callers pass a fixed value in tests and goldens.
    """
    before_keys = matching_keys(before.records)
    after_keys = matching_keys(after.records)
    before_by_key = {key: i for i, key in enumerate(before_keys)}
    after_by_key = {key: i for i, key in enumerate(after_keys)}
    before_bodies = _bodies(before)
    after_bodies = _bodies(after)

    changed: list[tuple[int, int]] = []
    removed: list[int] = []
    for key, i in before_by_key.items():
        j = after_by_key.get(key)
        if j is None:
            removed.append(i)
        elif _is_changed(
            before.records[i], after.records[j],
            before_bodies[i], after_bodies[j],
        ):
            changed.append((i, j))

    added = [j for key, j in after_by_key.items() if key not in before_by_key]

    rename_pairs, removed, added = _detect_renames(
        before, removed, before_bodies, after, added, after_bodies,
    )

    low = before.low_confidence or after.low_confidence

    added.sort(key=lambda j: record_sort_key(after.records[j]))
    removed.sort(key=lambda i: record_sort_key(before.records[i]))
    changed.sort(key=lambda pair: record_sort_key(after.records[pair[1]]))
    rename_pairs.sort(key=lambda pair: record_sort_key(after.records[pair[1]]))

    return {
        "repo": repo,
        "from": from_ref,
        "to": to_ref,
        "generated_at": generated_at if generated_at is not None else utc_now(),
        "added": [_record_dict(after.records[j], low) for j in added],
        "removed": [_record_dict(before.records[i], low) for i in removed],
        "changed": [
            {
                "before": _record_dict(before.records[i], low),
                "after": _record_dict(after.records[j], low),
                "body_diff": (
                    _unified_diff(
                        before.records[i], before_bodies[i],
                        after.records[j], after_bodies[j],
                    )
                    if before_bodies[i] is not None and after_bodies[j] is not None
                    else None
                ),
            }
            for i, j in changed
        ],
        "renamed": [
            {
                "before": _record_dict(before.records[i], low),
                "after": _record_dict(after.records[j], low),
            }
            for i, j in rename_pairs
        ],
    }


def surface_json_text(result: dict) -> str:
    """Canonical serialization: sorted keys, stable across runs (R16)."""
    return canonical_json(result)


def write_surface_json(result: dict, path: "str | Path") -> Path:
    """Write surface.json, creating the output directory if needed."""
    return write_text(path, surface_json_text(result))
