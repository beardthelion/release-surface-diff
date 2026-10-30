"""U5 tests: in-repo Python/TS route extractor (KD1, KTD12, R6, R7).

The fixture trees cover the acl-guard-audit blind spot: sink-free and
paramless handlers that still belong in the route inventory.
"""

import json
import os
import shlex
import stat
from pathlib import Path

import pytest

from release_surface_diff import extract
from release_surface_diff.extractors import py_ts_routes

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PY_BASIC = FIXTURES / "python" / "basic"
TS_BASIC = FIXTURES / "ts" / "basic"


def _ids(result):
    return {r.id for r in result.records}


def _by_id(result):
    out = {}
    for r in result.records:
        out.setdefault(r.id, []).append(r)
    return out


# ---- python pass ----


class TestPythonRoutes:
    def test_flask_fastapi_django_golden(self):
        res = py_ts_routes.extract(PY_BASIC)
        ids = _ids(res)
        assert {
            "GET /users/{id}",      # Flask <id> -> {id}
            "ANY /health",          # @app.route without methods
            "GET /legacy",          # add_url_rule methods=["GET"]
            "ANY /plain",           # add_url_rule without methods
            "POST /x",              # FastAPI decorator
            "GET /items/{item_id}", # FastAPI {name} form passes through
            "GET /multi",           # api_route methods=[...] expands
            "POST /multi",
            "ANY /a/{pk}",          # Django <int:pk> -> {pk}
            "ANY /old/{slug}",      # re_path (?P<slug>...) -> {slug}
            "ANY /classes/{name}",  # Django <slug:name> -> {name}
        } <= ids
        assert "ANY /v1" not in ids  # path("v1/", include(...)) is a mount
        assert len(ids) == len(res.records)  # no duplicate emissions

    def test_paramless_route_emits_record(self):
        """The acl-guard-audit blind spot: a sink-free paramless handler
        must still produce a route record."""
        res = py_ts_routes.extract(PY_BASIC)
        by_id = _by_id(res)
        assert "ANY /health" in by_id
        rec = by_id["ANY /health"][0]
        assert rec.kind == "route"
        assert rec.file == "app.py"
        assert rec.symbol == "health"
        assert rec.lang == "python"
        assert rec.extractor == py_ts_routes.EXTRACTOR

    def test_mounted_prefix_joins(self):
        res = py_ts_routes.extract(PY_BASIC)
        ids = _ids(res)
        assert "GET /api/status" in ids   # register_blueprint url_prefix
        assert "GET /v1/things" in ids    # include_router prefix
        assert "ANY /v1/x/{k}" in ids     # Django include() mount

    def test_symbol_table_spans(self):
        res = py_ts_routes.extract(PY_BASIC)
        spans = {(s.file, s.symbol): s for s in res.symbols}
        assert ("app.py", "get_user") in spans
        span = spans[("app.py", "get_user")]
        # the span is the def body (decorators excluded, so a path-only
        # change can still rename-match on body hash, KTD6)
        assert span.start_line < span.end_line
        by_id = _by_id(res)
        rec = by_id["GET /users/{id}"][0]
        assert rec.symbol == "get_user"

    def test_handler_record_points_at_decorator(self):
        res = py_ts_routes.extract(PY_BASIC)
        rec = _by_id(res)["GET /users/{id}"][0]
        src = (PY_BASIC / "app.py").read_text().splitlines()
        assert "@app.get" in src[rec.line - 1]

    def test_malformed_file_warns_not_crashes(self):
        res = py_ts_routes.extract(FIXTURES / "python" / "malformed")
        assert res.records == []
        assert any("broken.py" in w for w in res.warnings)


# ---- TS pass ----


class TestTsRoutes:
    def test_express_fastify_next_golden(self):
        res = py_ts_routes.extract(TS_BASIC)
        ids = _ids(res)
        assert {
            "GET /h/{id}",            # Express :id -> {id}
            "POST /items",
            "GET /r",                 # .route() chain
            "POST /r",
            "GET /api/things/{tid}",  # same-file .use mount
            "GET /f/{fid}",           # Fastify { method, url }
            "GET /multi",             # Fastify method array expands
            "POST /multi",
            "GET /direct/{did}",      # fastify verb shorthand
            "GET /api/health",        # Next app/**/route.ts + export GET
            "GET /users/{id}",        # Next [id] dir
            "POST /users/{id}",
            "ANY /api/legacy",        # pages/api file convention
        } <= ids
        assert "ANY /api" not in ids  # app.use("/api", api) is a mount
        assert len(ids) == len(res.records)  # no duplicate emissions

    def test_ts_paramless_and_methods(self):
        res = py_ts_routes.extract(TS_BASIC)
        by_id = _by_id(res)
        assert "POST /items" in by_id
        rec = by_id["POST /items"][0]
        assert rec.file == "server.ts"
        assert rec.symbol == "createItem"
        assert rec.lang == "ts"

    def test_use_middleware_lands_in_auth_hint(self):
        res = py_ts_routes.extract(TS_BASIC)
        by_id = _by_id(res)
        rec = by_id["GET /h/{id}"][0]
        assert rec.attrs.get("auth_hint") == ["requireAuth"]
        # mounted router var is not reported as middleware
        assert "api" not in rec.attrs["auth_hint"]

    def test_empty_file_yields_nothing(self):
        res = py_ts_routes.extract(TS_BASIC)
        assert all(r.file != "empty.ts" for r in res.records)

    def test_ts_symbol_span_best_effort(self):
        res = py_ts_routes.extract(TS_BASIC)
        spans = {(s.file, s.symbol) for s in res.symbols}
        assert ("server.ts", "getH") in spans


# ---- integration with the acl adapter (dedupe on kind+id) ----


def _chmod_x(path: Path) -> Path:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _make_acl_stub(bindir: Path, route="/users/{id}", method="GET",
                   path="app.py", function="get_user") -> Path:
    """A fake acl-guard-audit emitting one site for the fixture's Flask route."""
    payload = json.dumps(
        {
            "schema_version": "acl-guard-audit/v1",
            "ruleset_version": "0.3.0",
            "findings": [
                {
                    "site_id": f"{path}::{function}::0",
                    "path": path,
                    "function": function,
                    "method": method,
                    "route": route,
                    "entry_start": 8,
                    "test_path": False,
                }
            ],
            "recognized_complete": [],
            "candidates": [],
            "coverage": {"complete": True},
        }
    )
    out = bindir / "acl.out"
    out.write_text(payload)
    script = bindir / "acl-guard-audit"
    script.write_text(f"#!/bin/sh\ncat {shlex.quote(str(out))}\nexit 0\n")
    return _chmod_x(script)


class TestDedupeWithAcl:
    def test_shared_route_dedupes_on_kind_id(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RSD_ACL_GUARD_AUDIT", raising=False)
        _make_acl_stub(tmp_path)
        monkeypatch.setenv("RSD_ACL_GUARD_AUDIT", str(tmp_path / "acl-guard-audit"))
        res = extract.extract_tree(PY_BASIC, ("python",))
        matches = [r for r in res.records if (r.kind, r.id) == ("route", "GET /users/{id}")]
        assert len(matches) == 1
        # the in-repo record wins: it carries the symbol-table linkage
        assert matches[0].extractor == py_ts_routes.EXTRACTOR

    def test_acl_only_route_survives(self, tmp_path, monkeypatch):
        """A route acl saw but we did not is not dropped by dedupe."""
        monkeypatch.delenv("RSD_ACL_GUARD_AUDIT", raising=False)
        _make_acl_stub(tmp_path, route="/acl-only/{oid}", method="GET",
                       path="other.py", function="other")
        monkeypatch.setenv("RSD_ACL_GUARD_AUDIT", str(tmp_path / "acl-guard-audit"))
        res = extract.extract_tree(PY_BASIC, ("python",))
        ids = _ids(res)
        assert "GET /acl-only/{oid}" in ids
        assert "GET /users/{id}" in ids

    def test_union_loses_no_route(self, tmp_path, monkeypatch):
        """acl + in-repo union covers at least what either alone reports."""
        monkeypatch.delenv("RSD_ACL_GUARD_AUDIT", raising=False)
        _make_acl_stub(tmp_path)
        monkeypatch.setenv("RSD_ACL_GUARD_AUDIT", str(tmp_path / "acl-guard-audit"))
        res = extract.extract_tree(PY_BASIC, ("python",))
        ours = {r.id for r in py_ts_routes.extract(PY_BASIC).records}
        merged = {r.id for r in res.records if r.kind == "route"}
        assert ours <= merged
        assert "GET /users/{id}" in merged
