"""Shared adapter contract: bounded subprocess runs and the result shape.

Every external-tool adapter and helper runner returns an AdapterResult:
normalized records, the sidecars helpers emit, gap notes for the report's
coverage section, and warnings for stderr. A tool that is missing,
unparseable, or reports partial coverage degrades to notes plus an empty or
partial record list -- never an exception (KTD3, R17).
"""

import json
import subprocess
from dataclasses import dataclass, field

from ..records import CallEdge, Record, SymbolSpan

DEFAULT_TIMEOUT = 600.0

# Record attr marking output from a tool that reported partial coverage.
# Deltas involving a flagged record surface as low-confidence downstream
# (KTD3); U7 renders it in the report.
LOW_CONFIDENCE_ATTR = "low_confidence"


@dataclass
class AdapterResult:
    """What one extractor run yielded.

    `complete` is False when the tool was absent, failed, or reported partial
    coverage; gap_notes carries the human-readable reasons for report.md and
    warnings carries the stderr-facing lines.
    """

    records: list[Record] = field(default_factory=list)
    symbols: list[SymbolSpan] = field(default_factory=list)
    edges: list[CallEdge] = field(default_factory=list)
    gap_notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    complete: bool = True

    def merge(self, other: "AdapterResult") -> None:
        self.records += other.records
        self.symbols += other.symbols
        self.edges += other.edges
        self.gap_notes += other.gap_notes
        self.warnings += other.warnings
        self.complete = self.complete and other.complete

    def degrade(self, tool: str, reason: str) -> "AdapterResult":
        """The tool is unusable for this run: warning + gap note (R17)."""
        self.complete = False
        self.warnings.append(f"{tool}: {reason}")
        self.gap_notes.append(f"{tool}: {reason}")
        return self

    def flag_partial(self) -> None:
        """The tool ran but reported incomplete coverage: records already
        emitted carry the low-confidence flag (KTD3)."""
        self.complete = False
        for rec in self.records:
            rec.attrs.setdefault(LOW_CONFIDENCE_ATTR, True)


def run_tool(cmd, timeout=DEFAULT_TIMEOUT, cwd=None):
    """Run one extractor subprocess, bounded by `timeout` seconds.

    Returns (CompletedProcess, None) or (None, reason) when the process could
    not be launched or timed out. The caller decides whether the exit code
    matters; for black-box tools it does not (KTD3).
    """
    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
            errors="replace",
        )
    except FileNotFoundError:
        return None, "executable not found"
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout:g}s"
    except OSError as exc:
        return None, f"failed to launch: {exc}"
    return proc, None


def parse_stdout_json(proc):
    """Parse a tool's whole stdout as one JSON document.

    Success is parseable stdout, never the exit code (KTD3): egress-plane
    exits 0/2/3 with valid JSON, and acl-guard-audit exit 2 is ambiguous
    between operational failure and incomplete analysis. Returns
    (data, None) or (None, reason).
    """
    data = None
    if proc.stdout and proc.stdout.strip():
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = None
    if isinstance(data, dict):
        return data, None
    err_lines = (proc.stderr or "").strip().splitlines()
    detail = f"; stderr: {err_lines[0][:200]}" if err_lines else ""
    return (
        None,
        f"exited {proc.returncode} with no parseable JSON on stdout{detail}",
    )


def parse_jsonl(text):
    """Split stdout text into JSON objects: (objects, bad_line_count)."""
    items, bad = [], 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(obj, dict):
            items.append(obj)
        else:
            bad += 1
    return items, bad
