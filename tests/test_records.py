"""Normalized record contract: canonical ids, lang vocabulary, sidecars, ordinals."""

import json

import pytest

from release_surface_diff import records


def make(kind="route", id="GET /x", file="a.go", line=1, symbol="h", lang="go"):
    return records.Record(
        kind=kind, id=id, file=file, line=line, symbol=symbol,
        lang=lang, extractor="test@0", attrs={},
    )


# ---- canonical id builders (KTD5) ----


def test_route_id_method_and_template():
    assert records.route_id("get", "/users/{id}") == "GET /users/{id}"
    assert records.route_id("POST", "/x") == "POST /x"


def test_route_id_null_method_is_any():
    assert records.route_id(None, "/health") == "ANY /health"
    assert records.route_id("", "/health") == "ANY /health"


def test_rpc_id():
    assert records.rpc_id("eth_getProof") == "eth_getProof"


def test_cli_flag_id():
    assert records.cli_flag_id("--http.api") == "--http.api"
    assert records.cli_flag_id("http.api") == "--http.api"
    assert records.cli_flag_id("verbosity") == "--verbosity"


def test_config_key_id():
    assert records.config_key_id("server.http.port") == "server.http.port"
    assert records.config_key_id(["server", "http", "port"]) == "server.http.port"


def test_export_id():
    assert records.export_id("pkg.Foo") == "pkg.Foo"


def test_outbound_id():
    assert (
        records.outbound_id("http", "host", "api.example.com")
        == "http:host:api.example.com"
    )


def test_protocol_id_is_literal():
    lit = "/eth2/beacon_chain/req/status/1/ssz_snappy"
    assert records.protocol_id(lit) == lit


def test_canonical_id_builders():
    assert records.route_id("get", "/x") == "GET /x"
    assert records.rpc_id("m") == "m"
    assert records.cli_flag_id("f") == "--f"
    assert records.config_key_id("a.b") == "a.b"
    assert records.export_id("p.E") == "p.E"
    assert records.outbound_id("dns", "host", "h") == "dns:host:h"
    assert records.protocol_id("/p/1") == "/p/1"


# ---- vocabulary ----


def test_kind_and_lang_vocabulary():
    assert set(records.KINDS) == {
        "route", "rpc", "cli_flag", "config_key", "export", "outbound", "protocol_id",
    }
    assert set(records.LANGS) == {"go", "rust", "python", "ts"}


def test_normalize_lang_folds_js_variants():
    assert records.normalize_lang("go") == "go"
    assert records.normalize_lang("javascript") == "ts"
    assert records.normalize_lang("js") == "ts"
    assert records.normalize_lang("typescript") == "ts"
    assert records.normalize_lang("ts") == "ts"
    with pytest.raises(ValueError):
        records.normalize_lang("cobol")


# ---- record shape + serialization (R11, R16) ----


def test_record_shape_and_jsonl_roundtrip():
    rec = records.Record(
        kind="route", id="GET /api/v2/users/{id}", file="api.go", line=123,
        symbol="listUsers", lang="go", extractor="go-ast@0.1",
        attrs={"auth_hint": "requireAuth"},
    )
    d = rec.to_dict()
    assert d == {
        "kind": "route", "id": "GET /api/v2/users/{id}", "file": "api.go",
        "line": 123, "symbol": "listUsers", "lang": "go",
        "extractor": "go-ast@0.1", "attrs": {"auth_hint": "requireAuth"},
    }
    line = rec.to_json()
    assert json.loads(line) == d
    assert records.Record.from_dict(d) == rec


def test_record_from_dict_rejects_bad_shape():
    with pytest.raises(ValueError):
        records.Record.from_dict({"kind": "route"})
    with pytest.raises(ValueError):
        records.Record.from_dict({
            "kind": "bogus", "id": "x", "file": "f", "line": 1,
            "symbol": "s", "lang": "go", "extractor": "e",
        })


def test_record_sort_key_deterministic():
    recs = [
        make(kind="rpc", id="b", file="z.go"),
        make(kind="route", id="GET /b"),
        make(kind="route", id="GET /a"),
    ]
    ordered = sorted(recs, key=records.record_sort_key)
    assert [r.id for r in ordered] == ["GET /a", "GET /b", "b"]


# ---- ordinal disambiguation on (kind, id) collisions (KTD5) ----


def test_matching_keys_no_collision():
    recs = [make(id="GET /a"), make(kind="rpc", id="m")]
    keys = records.matching_keys(recs)
    assert keys == [("route", "GET /a", 0), ("rpc", "m", 0)]


def test_matching_keys_collision_ordinals_by_file_symbol():
    recs = [
        make(id="GET /a", file="b.go", symbol="hB"),
        make(id="GET /a", file="a.go", symbol="hA"),
        make(id="GET /a", file="a.go", symbol="hZ"),
        make(id="GET /other"),
    ]
    keys = records.matching_keys(recs)
    assert keys == [
        ("route", "GET /a", 2),   # b.go/hB sorts last by (file, symbol)
        ("route", "GET /a", 0),   # a.go/hA sorts first
        ("route", "GET /a", 1),   # a.go/hZ sorts second
        ("route", "GET /other", 0),
    ]


# ---- sidecar shapes (KTD7, KTD8) ----


def test_symbol_span_shape():
    span = records.SymbolSpan(file="srv.go", symbol="listUsers", start_line=10, end_line=42)
    assert span.to_dict() == {
        "file": "srv.go", "symbol": "listUsers", "start_line": 10, "end_line": 42,
    }


def test_call_edge_shape():
    edge = records.CallEdge(caller="main", callee="listUsers")
    assert edge.to_dict() == {"caller": "main", "callee": "listUsers"}
