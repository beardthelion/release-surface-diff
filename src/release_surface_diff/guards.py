"""Guard hunk classifier: `git diff -U5` -> classed, directed hunks (R13).

File entries in the diff are gated by the same PathMatcher that filters
extractor records (KTD11). Each surviving hunk is security-relevant when an
added or removed line matches one of the spec's pattern families; a hunk
matching several families takes the first class in the fixed precedence order
bounds, auth, allowlist, overflow, path, dispatch, other (KTD10). Direction
counts the +/- lines matching the winning class: only added lines -> `added`,
only removed -> `removed`, both -> `modified` (a guard rewritten in place).
`evidence` is the first line matching the winning class, sign included,
truncated at 200 chars. Enclosing-symbol attribution comes from the
extractor symbol tables (KTD7); the `@@` context string is the fallback.

Hunks are emitted in diff order (file order, then hunk order) so output is
deterministic across runs (R16).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from ._util import canonical_json, utc_now, write_text
from .config import PathMatcher
from .records import EXT_LANG, SymbolSpan

CLASSES = ("bounds", "auth", "allowlist", "overflow", "path", "dispatch", "other")

EVIDENCE_MAX = 200

# Spec ranking formula (guard hunk weight): a removed guard is a regression
# candidate and scores above an added one; a rewrite sits below both.
DIRECTION_WEIGHT = {"removed": 3, "added": 2, "modified": 1}

# Precedence order is load-bearing (KTD10): first matching family wins.
# Patterns match the line content after the +/- sign.
CLASS_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    (
        "bounds",
        re.compile(
            r"len\s*\(|\.len\s*\(|\.length\b|\.size\s*\("
            r"|\bmax_?\w*|\bmin_?\w*|\blimit\w*\b"
            r"|\b(?:size|count|depth|cap|capacity|num_\w+)\b\s*(?:<=|>=|<|>|==|!=)",
            re.IGNORECASE,
        ),
    ),
    (
        "auth",
        re.compile(
            r"\bauth\w*|\bcheck_perm\w*|\brequire_\w+|\bis_admin\b"
            r"|\bverif\w+|\bpermission\w*\b|\bmiddleware\b",
            re.IGNORECASE,
        ),
    ),
    (
        "allowlist",
        re.compile(
            r"\b(?:allow|deny|white|black)_?list\w*\b"
            r"|\ballowed_\w+|\bdenied_\w+|\bblocked_\w+",
            re.IGNORECASE,
        ),
    ),
    (
        "overflow",
        re.compile(
            r"\bchecked_\w+|\bsaturating_\w+|\bwrapping_\w+"
            r"|\boverflow\w*|\bunwrap\s*\(",
            re.IGNORECASE,
        ),
    ),
    (
        "path",
        re.compile(
            r"canonicaliz\w*|\bis_private\b|\bscheme\b|\bClean\s*\("
            r"|\bsanitiz\w*|\btraversal\b|\bhostname\b"
            r"|url\.Parse|urlparse|\bclean_path\b|\bis_safe_?path\b",
            re.IGNORECASE,
        ),
    ),
    (
        "dispatch",
        re.compile(
            r"^\s*(?:case|default)\b|\bmatch\b|\bswitch\b"
            r"|[\w:.'\"]+\s*=>|^\s*\w+\(.*\)\s*=>|=>\s*\{",
        ),
    ),
    (
        # Catch-all for guard-shaped edits outside the named families:
        # hard failures, denials, rate limits, validators.
        "other",
        re.compile(
            r"\bpanic\s*\(|\babort\s*\(|\bfatal\w*\b|\bdenied\b|\bdeny\b"
            r"|\bforbidden\b|\bunauthoriz\w*|\bunauthenticat\w*"
            r"|\bthrottl\w*|\brate_?limit\w*|\binsecure\b|\bvalidat\w+",
            re.IGNORECASE,
        ),
    ),
)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\s?(.*)$")


@dataclass
class GuardHunk:
    """One security-relevant diff hunk.

    `file` is the to-side path (old path for deletions); `old_file` is the
    a-side path and differs only on renames. old_/new_ ranges are the `@@`
    header coordinates (inclusive line ranges include context lines).
    """

    file: str
    old_file: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    hunk_class: str
    direction: str
    symbol: "str | None"
    evidence: str

    @property
    def weight(self) -> int:
        return DIRECTION_WEIGHT[self.direction]

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "class": self.hunk_class,
            "direction": self.direction,
            "symbol": self.symbol,
            "evidence": self.evidence,
            "weight": self.weight,
            "old_start": self.old_start,
            "old_end": self.old_start + self.old_count - 1,
            "new_start": self.new_start,
            "new_end": self.new_start + self.new_count - 1,
        }


@dataclass
class _FileDiff:
    old_path: "str | None" = None
    new_path: "str | None" = None
    hunks: list = field(default_factory=list)


@dataclass
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    context: str
    lines: list = field(default_factory=list)  # (sign, content) for +/- lines


# ---- diff parsing ----


def _unquote_git(s: str) -> str:
    """Undo git's C-style quoting of unusual paths in diff headers."""
    s = s.strip()
    if not (s.startswith('"') and s.endswith('"') and len(s) >= 2):
        return s
    body = s[1:-1]
    out = []
    i = 0
    escapes = {
        "n": "\n", "t": "\t", "r": "\r", "a": "\a",
        "b": "\b", "f": "\f", "v": "\v", "\\": "\\", '"': '"',
    }
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            n = body[i + 1]
            if n in "01234567":
                j = i + 1
                while j < len(body) and j < i + 4 and body[j] in "01234567":
                    j += 1
                out.append(chr(int(body[i + 1:j], 8)))
                i = j
                continue
            out.append(escapes.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _marker_path(line: str, prefix: str) -> "str | None":
    """Path from a `--- a/x` / `+++ b/x` header, or None for /dev/null."""
    rest = line[4:]
    if rest.startswith("/dev/null"):
        return None
    return _unquote_git(rest)[len(prefix):]


def parse_diff(diff_text: str) -> list[_FileDiff]:
    """Split diff text into per-file hunks with +/- line contents.

    Hunk bodies are consumed by line budget (old/new counts from the @@
    header), so a removed line whose text looks like a file header is not
    mistaken for one.
    """
    files: list[_FileDiff] = []
    cur: "_FileDiff | None" = None
    hunk: "_Hunk | None" = None
    old_rem = new_rem = 0

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            cur = _FileDiff()
            files.append(cur)
            hunk = None
            continue
        if hunk is None and line.startswith("--- "):
            if cur is not None:
                cur.old_path = _marker_path(line, "a/")
            continue
        if hunk is None and line.startswith("+++ "):
            if cur is not None:
                cur.new_path = _marker_path(line, "b/")
            continue
        m = _HUNK_RE.match(line)
        if m:
            hunk = _Hunk(
                old_start=int(m.group(1)),
                old_count=int(m.group(2) or 1),
                new_start=int(m.group(3)),
                new_count=int(m.group(4) or 1),
                context=m.group(5).strip(),
            )
            if cur is None:
                cur = _FileDiff()
                files.append(cur)
            cur.hunks.append(hunk)
            old_rem, new_rem = hunk.old_count, hunk.new_count
            continue
        if hunk is not None:
            sign = line[:1]
            if old_rem <= 0 and new_rem <= 0 and sign != "\\":
                hunk = None
                continue
            if sign == " ":
                old_rem -= 1
                new_rem -= 1
            elif sign == "-":
                old_rem -= 1
                hunk.lines.append(("-", line[1:]))
            elif sign == "+":
                new_rem -= 1
                hunk.lines.append(("+", line[1:]))
            # '\\' ("No newline at end of file") consumes no budget
            continue
    return files


# ---- classification ----


# A +/- line whose payload is only a comment carries no guard change:
# skip it for class, direction, and evidence. Covers //, #, /* and block
# continuation (*), <!-- -->, and SQL/Lua -- comment openers.
_COMMENT_PREFIXES = ("//", "#", "/*", "*", "<!--", "--")


def _is_comment(content: str) -> bool:
    return content.lstrip().startswith(_COMMENT_PREFIXES)


def _line_classes(content: str) -> list[str]:
    return [cls for cls, pat in CLASS_PATTERNS if pat.search(content)]


def _lang_ok(path: str, langs) -> bool:
    """Whether a file is in scope for `langs` via EXT_LANG. Files with no
    extension or an unmapped one stay in scope (coverage is not dropped
    silently); only a file mapped to a different lang is skipped."""
    if not langs:
        return True
    return EXT_LANG.get(Path(path).suffix.lower()) in langs


def _span_index(symbols) -> dict[str, list[SymbolSpan]]:
    index: dict[str, list[SymbolSpan]] = {}
    for span in symbols or ():
        index.setdefault(span.file, []).append(span)
    return index


def _attribute(hunk: _Hunk, file: str, old_file: str,
               index: dict[str, list[SymbolSpan]]) -> "str | None":
    """Tightest symbol-table span intersecting the hunk on either side."""
    spans = index.get(file, [])
    if old_file != file:
        spans = spans + index.get(old_file, [])
    new_lo, new_hi = hunk.new_start, hunk.new_start + max(hunk.new_count, 1) - 1
    old_lo, old_hi = hunk.old_start, hunk.old_start + max(hunk.old_count, 1) - 1
    cands = [
        s for s in spans
        if (s.start_line <= new_hi and s.end_line >= new_lo)
        or (s.start_line <= old_hi and s.end_line >= old_lo)
    ]
    if not cands:
        return None
    tightest = min(cands, key=lambda s: (s.end_line - s.start_line, s.start_line))
    return tightest.symbol


def classify_diff(
    diff_text: str,
    matcher: "PathMatcher | None" = None,
    symbols=None,
    symbols_from=None,
    langs=None,
) -> list[GuardHunk]:
    """Parse diff_text and return security-relevant hunks, in diff order.

    Only files passing `matcher` (default: PathMatcher defaults, KTD11) are
    scanned. `symbols` is the to-side extractor symbol table (KTD7), used
    for added and modified hunks; `symbols_from` is the from-side table,
    preferred for removed hunks so a renamed function does not attribute
    to a stale name. Without either, the `@@` context string is the
    enclosing-symbol fallback. When `langs` is given, files whose
    extension maps to a different language via EXT_LANG are skipped;
    unmapped extensions stay in scope.
    """
    matcher = matcher or PathMatcher()
    index = _span_index(symbols)
    index_from = (
        _span_index(symbols_from) if symbols_from is not None else index
    )
    out: list[GuardHunk] = []

    for f in parse_diff(diff_text):
        path = f.new_path if f.new_path is not None else f.old_path
        if path is None or not matcher.matches(path):
            continue
        if not _lang_ok(path, langs):
            continue
        old_path = f.old_path if f.old_path is not None else path
        for hunk in f.hunks:
            matched = [
                (s, c, _line_classes(c)) for s, c in hunk.lines
                if not _is_comment(c)
            ]
            matched = [(s, c, cs) for s, c, cs in matched if cs]
            if not matched:
                continue
            winning = next(
                cls for cls in CLASSES if any(cls in cs for _, _, cs in matched)
            )
            class_lines = [
                (s, c) for s, c, cs in matched if winning in cs
            ]
            signs = {s for s, _, _ in matched}
            if signs == {"+"}:
                direction = "added"
            elif signs == {"-"}:
                direction = "removed"
            else:
                direction = "modified"
            sign, content = class_lines[0]
            side = index_from if direction == "removed" else index
            symbol = _attribute(hunk, path, old_path, side) or hunk.context or None
            out.append(
                GuardHunk(
                    file=path,
                    old_file=old_path,
                    old_start=hunk.old_start,
                    old_count=hunk.old_count,
                    new_start=hunk.new_start,
                    new_count=hunk.new_count,
                    hunk_class=winning,
                    direction=direction,
                    symbol=symbol,
                    evidence=(sign + content)[:EVIDENCE_MAX],
                )
            )
    return out


# ---- guards.json emission (R14, R16) ----


def guards_json(
    hunks: list[GuardHunk],
    *,
    repo: str,
    from_ref: str,
    to_ref: str,
    generated_at: "str | None" = None,
) -> dict:
    """The guards.json document for one tag pair."""
    return {
        "repo": repo,
        "from": from_ref,
        "to": to_ref,
        "generated_at": generated_at if generated_at is not None else utc_now(),
        "hunks": [h.to_dict() for h in hunks],
    }


def guards_json_text(result: dict) -> str:
    """Canonical serialization: sorted keys, stable across runs (R16)."""
    return canonical_json(result)


def write_guards_json(result: dict, path: "str | Path") -> Path:
    """Write guards.json, creating the output directory if needed."""
    return write_text(path, guards_json_text(result))
