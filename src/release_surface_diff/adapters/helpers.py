"""Runner for the in-repo Go/Rust extractor helper binaries (KTD1, KTD2).

Invocation contract implemented by U4/U8:

    <binary> <root> --symbols <path> --edges <path>

The helper writes normalized records as JSONL on stdout (R11), the symbol
table at `--symbols`, and intra-package call edges at `--edges` (KTD7,
KTD8). A nonzero exit with parseable stdout is treated as partial output:
records are consumed and flagged low-confidence.

Resolution order per helper: explicit flag > env override
(RSD_GO_EXTRACTOR / RSD_RUST_EXTRACTOR) > bin/<name> in the source
checkout, rebuilt when the helper sources are newer than the binary
(KTD2). A missing binary or toolchain degrades to a warning plus a gap
note (R17).
"""

import os
import shutil
import tempfile
from pathlib import Path

from ..records import CallEdge, Record, SymbolSpan
from . import AdapterResult, parse_jsonl, run_tool

HELPER_TIMEOUT = 300.0
BUILD_TIMEOUT = 600.0

# Per-language build spec. `binary` names both bin/<name> and the release
# artifact cargo/go produce; `source` is the helpers/ subdirectory.
HELPERS = {
    "go": {
        "binary": "go-extractor",
        "env": "RSD_GO_EXTRACTOR",
        "source": "go-extractor",
        "toolchain": "go",
    },
    "rust": {
        "binary": "rust-extractor",
        "env": "RSD_RUST_EXTRACTOR",
        "source": "rust-extractor",
        "toolchain": "cargo",
    },
}


def _repo_root() -> Path:
    # src/release_surface_diff/adapters/helpers.py -> repo root
    return Path(__file__).resolve().parents[3]


def _sources_newer(src_dir: Path, binary: Path) -> bool:
    """Any file under the helper source tree newer than the binary."""
    if not src_dir.is_dir():
        return False
    try:
        mtime = binary.stat().st_mtime
    except OSError:
        return True
    # Skip build output and hidden dirs: target/ alone is thousands of
    # files on a rust helper that has been built once. The check runs on
    # the path relative to src_dir so a checkout under a dot-directory
    # does not disable staleness detection.
    for p in src_dir.rglob("*"):
        rel_parts = p.relative_to(src_dir).parts
        if any(part.startswith(".") or part == "target" for part in rel_parts):
            continue
        if p.is_file():
            try:
                if p.stat().st_mtime > mtime:
                    return True
            except OSError:
                continue
    return False


def _build(lang: str, src_dir: Path, binary: Path) -> str | None:
    """Build the helper into bin/. Returns an error string or None."""
    spec = HELPERS[lang]
    if shutil.which(spec["toolchain"]) is None:
        return f"{spec['toolchain']} toolchain not on PATH; cannot build {spec['binary']}"
    try:
        binary.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"cannot create {binary.parent}: {exc}"
    if lang == "go":
        cmd = [spec["toolchain"], "build", "-o", str(binary), "."]
        artifact = binary
    else:
        cmd = [spec["toolchain"], "build", "--release"]
        artifact = src_dir / "target" / "release" / spec["binary"]
    proc, err = run_tool(cmd, timeout=BUILD_TIMEOUT, cwd=src_dir)
    if err:
        return f"{spec['binary']} build failed: {err}"
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        tail = lines[-1][:200] if lines else "no output"
        return f"{spec['binary']} build failed: {tail}"
    if lang == "rust":
        if not artifact.is_file():
            return f"cargo build produced no {artifact.name}"
        try:
            shutil.copy2(artifact, binary)
        except OSError as exc:
            return f"cannot copy {artifact.name} into {binary.parent}: {exc}"
    return None


def resolve(lang: str, explicit=None):
    """Find a usable helper binary: (path, reason-if-missing).

    explicit > env override > bin/<name> (built when absent or stale).
    """
    spec = HELPERS[lang]
    candidate = explicit or os.environ.get(spec["env"])
    if candidate:
        p = Path(candidate)
        if p.is_file() and os.access(p, os.X_OK):
            return p, None
        origin = "flag" if explicit else spec["env"]
        return None, f"{origin} points at {candidate} which is not an executable file"
    root = _repo_root()
    binary = root / "bin" / spec["binary"]
    src_dir = root / "helpers" / spec["source"]
    if binary.is_file() and os.access(binary, os.X_OK) and not _sources_newer(src_dir, binary):
        return binary, None
    if not src_dir.is_dir():
        return (
            None,
            f"{spec['binary']} not built and helper source tree "
            f"helpers/{spec['source']} is absent",
        )
    err = _build(lang, src_dir, binary)
    if err:
        # A stale-but-present binary still beats no coverage; say so.
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary, f"rebuild failed ({err}); using stale binary"
        return None, err
    return binary, None


def _read_sidecar(path: Path):
    """JSONL sidecar -> (items, bad_count); bad_count -1 when absent."""
    try:
        return parse_jsonl(path.read_text(errors="replace"))
    except OSError:
        return [], -1


def run_helper(lang: str, root, *, explicit=None, timeout=HELPER_TIMEOUT) -> AdapterResult:
    """Run the lang helper against `root` and collect records + sidecars."""
    result = AdapterResult()
    spec = HELPERS.get(lang)
    if spec is None:
        return result.degrade(f"{lang}-extractor", f"no helper defined for lang {lang!r}")
    binary, why = resolve(lang, explicit)
    if binary is None:
        return result.degrade(spec["binary"], why or "unavailable")
    if why:  # stale-binary fallback warning
        result.warnings.append(f"{spec['binary']}: {why}")
    with tempfile.TemporaryDirectory(prefix=f"rsd-{lang}-helper-") as side_dir:
        sym_path = Path(side_dir) / "symbols.jsonl"
        edge_path = Path(side_dir) / "edges.jsonl"
        proc, err = run_tool(
            [binary, str(root), "--symbols", str(sym_path), "--edges", str(edge_path)],
            timeout,
        )
        if err:
            return result.degrade(spec["binary"], err)
        side_symbols, bad_sym = _read_sidecar(sym_path)
        side_edges, bad_edge = _read_sidecar(edge_path)
    objs, bad = parse_jsonl(proc.stdout)
    for obj in objs:
        try:
            result.records.append(Record.from_dict(obj))
        except (ValueError, TypeError, KeyError) as exc:
            result.warnings.append(f"{spec['binary']}: skipped malformed record: {exc}")
    if bad:
        result.warnings.append(
            f"{spec['binary']}: {bad} stdout line(s) were not JSON objects"
        )
    if not objs and (proc.returncode != 0 or (proc.stdout or "").strip()):
        return result.degrade(
            spec["binary"],
            f"exited {proc.returncode}; stdout was not parseable record JSONL",
        )
    if proc.returncode != 0:
        result.warnings.append(
            f"{spec['binary']}: exited {proc.returncode}; records treated as partial"
        )
        result.gap_notes.append(
            f"{spec['binary']}: exited {proc.returncode}; records may be partial"
        )
        result.flag_partial()
    symbols, edges = side_symbols, side_edges
    for s in symbols:
        result.symbols.append(
            SymbolSpan(
                file=str(s.get("file") or ""),
                symbol=str(s.get("symbol") or ""),
                start_line=int(s.get("start_line") or 0),
                end_line=int(s.get("end_line") or 0),
            )
        )
    for e in edges:
        result.edges.append(
            CallEdge(caller=str(e.get("caller") or ""), callee=str(e.get("callee") or ""))
        )
    if bad_sym == -1:
        result.gap_notes.append(
            f"{spec['binary']}: no symbol-table sidecar; body attribution degraded"
        )
    if bad_edge == -1:
        result.gap_notes.append(
            f"{spec['binary']}: no call-edge sidecar; reach scoring degraded"
        )
    for label, count in (("symbol", bad_sym), ("edge", bad_edge)):
        if count and count > 0:
            result.warnings.append(
                f"{spec['binary']}: {count} malformed {label} sidecar line(s)"
            )
    return result
