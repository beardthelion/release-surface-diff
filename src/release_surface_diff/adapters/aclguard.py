"""Adapter for `acl-guard-audit scan <root> --json` (R6, KTD3).

Every site in `findings`, `recognized_complete`, and `candidates` maps to a
`route` record: acl-guard-audit's sites are exactly the handlers it could
see, which is the surface we inventory. Verdict fields (state, classes,
id_params, rw, confidence) are ignored -- this adapter consumes locations,
not judgments. Exit code 2 is ambiguous between operational failure and
incomplete analysis, so parseable stdout is consumed regardless (KTD3).
"""

from pathlib import Path

from ..records import Record, route_id
from . import DEFAULT_TIMEOUT, AdapterResult, parse_stdout_json, run_tool

TOOL_NAME = "acl-guard-audit"
ENV_VAR = "RSD_ACL_GUARD_AUDIT"
SCHEMA_VERSION = "acl-guard-audit/v1"
SCAN_TIMEOUT = DEFAULT_TIMEOUT

_SITE_BUCKETS = ("findings", "recognized_complete", "candidates")

_EXT_LANG = {
    ".py": "python",
    ".ts": "ts",
    ".tsx": "ts",
    ".js": "ts",
    ".jsx": "ts",
    ".mjs": "ts",
    ".cjs": "ts",
}


def _extractor_name(data: dict) -> str:
    version = data.get("ruleset_version")
    return f"{TOOL_NAME}@{version}" if version else TOOL_NAME


def _site_record(site, extractor: str):
    """One acl site -> one route record.

    Returns (record, gap_note_or_None). Sites under test paths are dropped
    like any excluded file (KTD11); a site with no route emits nothing and
    is reported as a gap per R17.
    """
    if not isinstance(site, dict):
        return None, f"{TOOL_NAME}: a site entry is not an object"
    if site.get("test_path"):
        return None, None
    path = site.get("path") or ""
    route = site.get("route")
    if not route:
        return None, (
            f"{TOOL_NAME}: site {site.get('site_id', '?')} at {path} has no "
            f"route; handler {site.get('function', '?')} not inventoried"
        )
    lang = _EXT_LANG.get(Path(path).suffix.lower())
    if lang is None:
        return None, (
            f"{TOOL_NAME}: site {site.get('site_id', '?')} at {path} has an "
            "extension with no canonical lang"
        )
    try:
        line = int(site.get("entry_start") or 0)
    except (TypeError, ValueError):
        line = 0
    return (
        Record(
            kind="route",
            id=route_id(site.get("method"), str(route)),
            file=str(path),
            line=line,
            symbol=str(site.get("function") or ""),
            lang=lang,
            extractor=extractor,
            attrs={},
        ),
        None,
    )


def _coverage_notes(data: dict) -> tuple[list[str], bool]:
    """(gap notes, complete) from the payload's coverage block."""
    notes: list[str] = []
    cov = data.get("coverage") or {}
    complete = bool(cov.get("complete", True))
    if not complete:
        verdict = cov.get("verdict")
        notes.append(
            f"{TOOL_NAME}: analysis incomplete"
            + (f" ({verdict})" if verdict else "")
        )
    for reason in cov.get("incomplete_reasons") or []:
        notes.append(f"{TOOL_NAME}: incomplete: {reason}")
    for warning in cov.get("warnings") or []:
        notes.append(f"{TOOL_NAME}: {warning}")
    return notes, complete


def scan(root, executable, *, timeout=SCAN_TIMEOUT) -> AdapterResult:
    """Run acl-guard-audit against `root` and map sites to route records.

    `executable` is the resolved binary path; None degrades per R17.
    """
    result = AdapterResult()
    if executable is None:
        return result.degrade(
            TOOL_NAME,
            "binary not found (set --acl-guard-audit or RSD_ACL_GUARD_AUDIT, "
            "or put acl-guard-audit on PATH)",
        )
    proc, err = run_tool([executable, "scan", str(root), "--json"], timeout)
    if err:
        return result.degrade(TOOL_NAME, err)
    data, err = parse_stdout_json(proc)
    if err:
        return result.degrade(TOOL_NAME, err)
    if data.get("schema_version") != SCHEMA_VERSION:
        return result.degrade(
            TOOL_NAME,
            f"schema_version {data.get('schema_version')!r} does not match "
            f"expected {SCHEMA_VERSION!r}; adapter does not trust a drifted shape",
        )
    extractor = _extractor_name(data)
    dropped_test = 0
    for bucket in _SITE_BUCKETS:
        for site in data.get(bucket) or []:
            rec, note = _site_record(site, extractor)
            if rec is not None:
                result.records.append(rec)
            elif note:
                result.gap_notes.append(note)
            else:
                dropped_test += 1
    if dropped_test:
        result.gap_notes.append(
            f"{TOOL_NAME}: {dropped_test} site(s) under test paths excluded"
        )
    notes, complete = _coverage_notes(data)
    result.gap_notes += notes
    if not complete:
        result.flag_partial()
    return result
