"""Normalized surface record contract (R11) and canonical id builders (KTD5).

Every extractor, helper, and adapter emits the same record shape:

    {"kind": ..., "id": ..., "file": ..., "line": ..., "symbol": ...,
     "lang": ..., "extractor": ..., "attrs": {...}}

`id` is composed canonically per kind so two extractors describing the same
entry point produce the same key. SymbolSpan and CallEdge are the sidecar
shapes helpers emit alongside records: symbol spans drive body attribution
and body_diff, call edges drive the reach bonus.
"""

import json
from dataclasses import dataclass, field

KINDS = ("route", "rpc", "cli_flag", "config_key", "export", "outbound", "protocol_id")

# Canonical lang tokens; JS variants fold into ts (KTD5).
LANGS = ("go", "rust", "python", "ts")

EXT_LANG = {
    ".go": "go",
    ".rs": "rust",
    ".py": "python",
    ".ts": "ts",
    ".tsx": "ts",
    ".js": "ts",
    ".jsx": "ts",
    ".mjs": "ts",
    ".cjs": "ts",
}

_LANG_ALIASES = {
    "go": "go",
    "golang": "go",
    "rust": "rust",
    "rs": "rust",
    "python": "python",
    "py": "python",
    "ts": "ts",
    "typescript": "ts",
    "tsx": "ts",
    "js": "ts",
    "jsx": "ts",
    "javascript": "ts",
    "node": "ts",
}

RECORD_FIELDS = ("kind", "id", "file", "line", "symbol", "lang", "extractor", "attrs")


def normalize_lang(value: str) -> str:
    """Fold a language token into the canonical go|rust|python|ts vocabulary."""
    lang = _LANG_ALIASES.get(value.strip().lower())
    if lang is None:
        raise ValueError(f"unknown language {value!r}; expected one of {LANGS}")
    return lang


# ---- canonical id builders (KTD5) ----


def route_id(method: str | None, template: str) -> str:
    """`METHOD {template}`; `ANY {template}` when the source method is null."""
    verb = (method or "").strip().upper() or "ANY"
    return f"{verb} {template.strip()}"


def rpc_id(name: str) -> str:
    return name.strip()


def cli_flag_id(name: str) -> str:
    """`--name`; leading dashes in the source spelling are normalized away."""
    stripped = name.strip().lstrip("-")
    if not stripped:
        raise ValueError("cli_flag id needs a flag name")
    return f"--{stripped}"


def config_key_id(path) -> str:
    """Dotted path; accepts a dotted string or an iterable of segments."""
    if isinstance(path, str):
        return path.strip()
    return ".".join(str(part).strip() for part in path)


def export_id(qualified: str) -> str:
    return qualified.strip()


def outbound_id(mechanism: str, dest_kind: str, dest_value: str) -> str:
    """`mechanism:dest-kind:dest-value` (e.g. http:host:api.example.com)."""
    return f"{mechanism.strip()}:{dest_kind.strip()}:{dest_value.strip()}"


def protocol_id(literal: str) -> str:
    """The literal is the id (e.g. /eth2/beacon_chain/req/status/1/ssz_snappy)."""
    return literal.strip()


def canonical_id(kind: str, **kw) -> str:
    """Dispatch to the per-kind id builder."""
    if kind == "route":
        return route_id(kw.get("method"), kw["template"])
    if kind == "rpc":
        return rpc_id(kw["name"])
    if kind == "cli_flag":
        return cli_flag_id(kw["name"])
    if kind == "config_key":
        return config_key_id(kw["path"])
    if kind == "export":
        return export_id(kw["qualified"])
    if kind == "outbound":
        return outbound_id(kw["mechanism"], kw["dest_kind"], kw["dest_value"])
    if kind == "protocol_id":
        return protocol_id(kw["literal"])
    raise ValueError(f"unknown record kind {kind!r}; expected one of {KINDS}")


# ---- normalized record (R11) ----


@dataclass
class Record:
    kind: str
    id: str
    file: str
    line: int
    symbol: str
    lang: str
    extractor: str
    attrs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "id": self.id,
            "file": self.file,
            "line": self.line,
            "symbol": self.symbol,
            "lang": self.lang,
            "extractor": self.extractor,
            "attrs": dict(sorted(self.attrs.items())),
        }

    def to_json(self) -> str:
        """One JSONL line; sorted keys keep output deterministic (R16)."""
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict) -> "Record":
        missing = [f for f in RECORD_FIELDS if f not in data]
        if missing:
            raise ValueError(f"record missing field(s): {', '.join(missing)}")
        if data["kind"] not in KINDS:
            raise ValueError(f"unknown record kind {data['kind']!r}")
        return cls(
            kind=data["kind"],
            id=data["id"],
            file=data["file"],
            line=int(data["line"]),
            symbol=data["symbol"],
            lang=normalize_lang(data["lang"]),
            extractor=data["extractor"],
            attrs=dict(data["attrs"]),
        )


def record_sort_key(rec: Record) -> tuple:
    """Deterministic ordering across runs (R16): kind, id, then position."""
    return (rec.kind, rec.id, rec.file, rec.line, rec.symbol)


def matching_keys(items: list[Record]) -> list[tuple[str, str, int]]:
    """(kind, id, ordinal) per record, in input order.

    Two records in one snapshot sharing (kind, id) are disambiguated by the
    sorted ordinal of (file, symbol) within their collision group (KTD5);
    unique records get ordinal 0.
    """
    groups: dict[tuple[str, str], list[int]] = {}
    for i, rec in enumerate(items):
        groups.setdefault((rec.kind, rec.id), []).append(i)
    keys: list[tuple[str, str, int]] = [(rec.kind, rec.id, 0) for rec in items]
    for idxs in groups.values():
        if len(idxs) == 1:
            continue
        ranked = sorted(idxs, key=lambda i: (items[i].file, items[i].symbol))
        for ordinal, i in enumerate(ranked):
            keys[i] = (items[i].kind, items[i].id, ordinal)
    return keys


# ---- extractor sidecars (KTD7, KTD8) ----


@dataclass
class SymbolSpan:
    """(file, symbol, start_line, end_line): enclosing-symbol ranges for
    guard-hunk attribution, body_diff extraction, and body hashing."""

    file: str
    symbol: str
    start_line: int
    end_line: int

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "symbol": self.symbol,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


@dataclass
class CallEdge:
    """(caller, callee) resolvable within the same package/module; feeds the
    reach bonus for hunks under symbols reachable from a changed entry point."""

    caller: str
    callee: str

    def to_dict(self) -> dict:
        return {"caller": self.caller, "callee": self.callee}
