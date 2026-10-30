"""Bare-literal protocol-id pass (R10, KTD12).

Single producer for the consensus/libp2p protocol surface: req/resp
protocol strings (``/eth2/beacon_chain/req/status/1/ssz_snappy``), libp2p
protocol ids (``/meshsub/1.1.0``, ``/ipfs/id/1.0.0``), and gossip topic
templates (``/eth2/%x/beacon_block/ssz_snappy``, ``beacon_attestation_%d``).

The helper binaries emit protocol ids only for structured registrations
with non-literal arguments; every bare literal, in every language, comes
from this pass so each literal yields exactly one record (U5/U8 split).

Entry point: ``extract(root) -> AdapterResult``.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..adapters import AdapterResult
from ..config import PathMatcher
from ..records import EXT_LANG, Record

EXTRACTOR = "protocol-ids"

# Quoted literals on a single line. Protocol ids never contain quotes, so
# escape handling is not needed.
_STRING_RE = re.compile(r"(['\"`])([^'\"`\n]*)\1")

_SEG = r"[A-Za-z0-9_.{}%$*-]+"
# /eth2/beacon_chain/req/status/1/ssz_snappy and friends: the req/resp
# marker segment is the strongest signal and cannot collide with URL
# paths, which never carry it.
_REQ_RESP_RE = re.compile(
    rf"^/{_SEG}(?:/{_SEG})*/(?:req|resp)(?:/{_SEG})+$"
)
# Consensus-namespaced gossip/topic literals: /eth2/<digest>/beacon_block,
# /eth2/%x/voluntary_exit/ssz_snappy, ...
_ETH2_RE = re.compile(rf"^/eth2(?:/{_SEG})+$")
# Generic libp2p id form: /proto/sub/1.0.0 -- a dotted-semver tail. URL
# paths essentially never end in x.y.z, so this separates protocol ids
# from route templates.
_SEMVER_TAIL_RE = re.compile(
    rf"^/[a-z][a-z0-9_-]*(?:/{_SEG})*/\d+\.\d+\.\d+(?:/{_SEG})*$"
)
# Bare gossip topic templates with no path prefix: beacon_attestation_%d,
# blob_sidecar_{subnet}, sync_committee_3, ...
_TOPIC_RE = re.compile(
    r"^(?:beacon_block|beacon_aggregate_and_proof|beacon_attestation"
    r"|voluntary_exit|proposer_slashing|attester_slashing"
    r"|sync_committee_contribution_and_proof|sync_committee"
    r"|bls_to_execution_change|blob_sidecar|beacon_blocks_by_range"
    r"|beacon_blocks_by_root|beacon_block_and_blobs"
    r"|light_client_[a-z_]+)"
    r"(?:_[a-z0-9_{}%d]*)?$"
)

_PATTERNS = (_REQ_RESP_RE, _ETH2_RE, _SEMVER_TAIL_RE, _TOPIC_RE)

# Per-lang "function starts here" regexes for best-effort enclosing-symbol
# attribution; keyed to the nearest preceding definition.
_DEF_RES = {
    "go": re.compile(
        r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("
    ),
    "rust": re.compile(
        r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)"
    ),
    "python": re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"),
    "ts": re.compile(
        r"\b(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|"
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
        r"(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
    ),
}


def _is_protocol_literal(lit: str) -> bool:
    lit = lit.strip()
    if not lit or len(lit) < 3 or " " in lit:
        return False
    return any(p.match(lit) for p in _PATTERNS)


def _symbols_by_line(lines: list[str], lang: str) -> list[tuple[int, str]]:
    pat = _DEF_RES.get(lang)
    if pat is None:
        return []
    out = []
    for i, line in enumerate(lines, start=1):
        m = pat.search(line)
        if m:
            name = next((g for g in m.groups() if g), "")
            if name:
                out.append((i, name))
    return out


def _extract_file(rel: str, text: str, lang: str, result: AdapterResult):
    lines = text.splitlines()
    defs = _symbols_by_line(lines, lang)
    seen: set[tuple[str, str]] = set()
    for lineno, line in enumerate(lines, start=1):
        for m in _STRING_RE.finditer(line):
            lit = m.group(2).strip()
            if not _is_protocol_literal(lit):
                continue
            if (rel, lit) in seen:
                continue
            seen.add((rel, lit))
            symbol = ""
            for def_line, name in defs:
                if def_line <= lineno:
                    symbol = name
                else:
                    break
            result.records.append(
                Record(
                    kind="protocol_id",
                    id=lit,
                    file=rel,
                    line=lineno,
                    symbol=symbol,
                    lang=lang,
                    extractor=EXTRACTOR,
                    attrs={},
                )
            )


def extract(root) -> AdapterResult:
    """Scan every source file under ``root`` for protocol-id literals."""
    result = AdapterResult()
    root = Path(root)
    matcher = PathMatcher()
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        lang = EXT_LANG.get(f.suffix.lower())
        if lang is None:
            continue
        rel = f.relative_to(root).as_posix()
        if not matcher.matches(rel):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            result.warnings.append(f"{EXTRACTOR}: unreadable file {rel}: {exc}")
            continue
        _extract_file(rel, text, lang, result)
    return result
