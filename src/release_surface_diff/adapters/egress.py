"""Adapter for `egress-plane scan --format json` (R5, KTD3).

The binary is a black box: stdout JSON is consumed whenever it parses, on
any exit code (0 complete, 2 incomplete, 3 empty; 1 is a tool error with no
JSON). Sites map to `outbound` records per the plan's field mapping; the
completeness block and blind spots become report gap notes, and a scan that
reports itself incomplete flags its records low-confidence.
"""

from ..records import Record, normalize_lang, outbound_id
from . import DEFAULT_TIMEOUT, AdapterResult, parse_stdout_json, run_tool

TOOL_NAME = "egress-plane"
ENV_VAR = "RSD_EGRESS_PLANE"
SCHEMA_VERSION = 1
SCAN_TIMEOUT = DEFAULT_TIMEOUT

# Site labels that act as exclude signals under the KTD11 filter: the tool
# already classified the file, so the record is dropped even where path
# globs would have kept it.
EXCLUDED_LABELS = frozenset({"test", "vendored", "generated", "minified", "build-hook"})


def _extractor_name(data: dict) -> str:
    tool = data.get("tool") or {}
    version = tool.get("version")
    return f"{TOOL_NAME}@{version}" if version else TOOL_NAME


def _site_record(site, extractor: str):
    """One egress site -> one outbound record.

    Returns (record, reason-skipped-or-None). A site dropped by its labels
    returns (None, None): routine filtering, not a defect.
    """
    if not isinstance(site, dict):
        return None, "a site entry is not an object"
    if EXCLUDED_LABELS.intersection(site.get("labels") or []):
        return None, None
    dest = site.get("destination") or {}
    file = site.get("file")
    mechanism = str(site.get("mechanism") or "").strip()
    dkind = str(dest.get("kind") or "").strip()
    dvalue = str(dest.get("value") or "").strip()
    if not file or not mechanism or not dkind or not dvalue:
        return None, f"site {site.get('id', '?')} lacks file/mechanism/destination"
    try:
        lang = normalize_lang(str(site.get("language") or ""))
    except ValueError:
        return None, f"site {site.get('id', '?')} has unknown language {site.get('language')!r}"
    loc = site.get("location")
    line = int(loc["line"]) if isinstance(loc, dict) and loc.get("line") else 0
    return (
        Record(
            kind="outbound",
            id=outbound_id(mechanism, dkind, dvalue),
            file=str(file),
            line=line,
            symbol=str(site.get("symbol") or ""),
            lang=lang,
            extractor=extractor,
            attrs={},
        ),
        None,
    )


def _completeness_notes(data: dict) -> list[str]:
    """The payload's completeness block and blind spots as gap notes."""
    notes = []
    comp = data.get("completeness") or {}
    state = comp.get("state")
    if state and state != "complete":
        budget = comp.get("budget_exceeded") or []
        extra = f" (budget exceeded: {', '.join(str(b) for b in budget)})" if budget else ""
        notes.append(f"{TOOL_NAME}: scan completeness={state}{extra}")
    coverage = comp.get("coverage") or {}
    skipped = [
        f"{lang}={counts.get('skipped')}"
        for lang, counts in sorted(coverage.items())
        if isinstance(counts, dict) and counts.get("skipped")
    ]
    if skipped:
        notes.append(f"{TOOL_NAME}: files skipped by the scanner: {', '.join(skipped)}")
    for spot in data.get("blind_spots") or []:
        if not isinstance(spot, dict):
            continue
        where = str(spot.get("file") or "?")
        loc = spot.get("location")
        if isinstance(loc, dict) and loc.get("line"):
            where += f":{loc['line']}"
        sym = spot.get("symbol") or ""
        notes.append(
            f"{TOOL_NAME} blind spot: {spot.get('kind', '?')} at {where}"
            + (f" ({sym})" if sym else "")
        )
    undeps = data.get("unscanned_dependencies") or []
    if undeps:
        notes.append(f"{TOOL_NAME}: {len(undeps)} dependency root(s) left unscanned")
    return notes


def scan(root, executable, *, timeout=SCAN_TIMEOUT) -> AdapterResult:
    """Run egress-plane against `root` and map sites to outbound records.

    `executable` is the resolved binary path; None degrades per R17.
    """
    result = AdapterResult()
    if executable is None:
        return result.degrade(
            TOOL_NAME,
            "binary not found (set --egress-plane or RSD_EGRESS_PLANE, "
            "or put egress-plane on PATH)",
        )
    proc, err = run_tool([executable, "scan", "--format", "json", str(root)], timeout)
    if err:
        return result.degrade(TOOL_NAME, err)
    data, err = parse_stdout_json(proc)
    if err:
        return result.degrade(TOOL_NAME, err)
    if data.get("schema_version") != SCHEMA_VERSION:
        return result.degrade(
            TOOL_NAME,
            f"schema_version {data.get('schema_version')!r} does not match "
            f"expected {SCHEMA_VERSION}; adapter does not trust a drifted shape",
        )
    extractor = _extractor_name(data)
    dropped_labels = 0
    for site in data.get("sites") or []:
        rec, why = _site_record(site, extractor)
        if rec is not None:
            result.records.append(rec)
        elif why:
            result.warnings.append(f"{TOOL_NAME}: skipped {why}")
        else:
            dropped_labels += 1
    if dropped_labels:
        result.gap_notes.append(
            f"{TOOL_NAME}: {dropped_labels} site(s) excluded by tool labels "
            "(test/vendored/generated/minified/build-hook)"
        )
    result.gap_notes += _completeness_notes(data)
    if (data.get("completeness") or {}).get("state") == "incomplete":
        # 0/2/3 all carry valid JSON; only `incomplete` marks the side partial.
        result.flag_partial()
    return result
