"""Adapter and tool-discovery tests (U2).

Fake executables on a stubbed PATH stand in for egress-plane,
acl-guard-audit, and the helper binaries. The success test everywhere is
parseable stdout JSON, never the exit code (KTD3).
"""

import json
import os
import shlex
import shutil
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from release_surface_diff import cli, extract
from release_surface_diff.adapters import aclguard, egress, helpers
from release_surface_diff.config import PathMatcher

SRC = Path(__file__).resolve().parents[1] / "src"


# ---- fake-executable builders ----


def _chmod_x(path: Path) -> Path:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def make_tool(bindir: Path, name: str, stdout: str, code: int = 0, stderr: str = "") -> Path:
    """A script that prints `stdout` verbatim and exits `code`."""
    payload = bindir / f"{name}.out"
    payload.write_text(stdout)
    lines = ["#!/bin/sh", f"cat {shlex.quote(str(payload))}"]
    if stderr:
        lines.append(f"echo {shlex.quote(stderr)} >&2")
    lines.append(f"exit {code}")
    script = bindir / name
    script.write_text("\n".join(lines) + "\n")
    return _chmod_x(script)


def make_helper(
    bindir: Path,
    name: str,
    records: list[dict],
    symbols: list[dict] | None = None,
    edges: list[dict] | None = None,
    code: int = 0,
) -> Path:
    """A script honoring `<root> --symbols <path> --edges <path>`.

    Prints the record JSONL and cats canned sidecar content into whatever
    paths the flags name, so the runner's flag contract is exercised.
    """
    rec_file = bindir / f"{name}.records"
    rec_file.write_text("".join(json.dumps(r) + "\n" for r in records))
    sym_file = bindir / f"{name}.symbols"
    sym_file.write_text("".join(json.dumps(s) + "\n" for s in (symbols or [])))
    edge_file = bindir / f"{name}.edges"
    edge_file.write_text("".join(json.dumps(e) + "\n" for e in (edges or [])))
    script = bindir / name
    script.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                'sym=""; edg=""',
                "while [ $# -gt 0 ]; do",
                '  case "$1" in',
                '    --symbols) sym="$2"; shift 2;;',
                '    --edges) edg="$2"; shift 2;;',
                "    *) shift;;",
                "  esac",
                "done",
                f"cat {shlex.quote(str(rec_file))}",
                f'[ -n "$sym" ] && cat {shlex.quote(str(sym_file))} > "$sym"',
                f'[ -n "$edg" ] && cat {shlex.quote(str(edge_file))} > "$edg"',
                f"exit {code}",
            ]
        )
        + "\n"
    )
    return _chmod_x(script)


@pytest.fixture
def stub_path(tmp_path, monkeypatch):
    """PATH = stub dir + dirs holding git/python/bash only, and clear tool
    env overrides, so a system-installed egress-plane or acl-guard-audit can
    never leak into these tests."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for var in (
        "RSD_EGRESS_PLANE",
        "RSD_ACL_GUARD_AUDIT",
        "RSD_GO_EXTRACTOR",
        "RSD_RUST_EXTRACTOR",
    ):
        monkeypatch.delenv(var, raising=False)
    keep = [str(bindir)]
    for tool in ("git", "python3", "python", "bash", "sh"):
        found = shutil.which(tool)
        if found:
            d = os.path.dirname(found)
            if d not in keep:
                keep.append(d)
    monkeypatch.setenv("PATH", os.pathsep.join(keep))
    return bindir


@pytest.fixture
def root(tmp_path):
    d = tmp_path / "tree"
    d.mkdir()
    (d / "main.py").write_text("# fixture\n")
    return d


# ---- fixture payloads ----


def egress_payload(sites=(), state="complete", blind_spots=(), budget=()):
    return json.dumps(
        {
            "schema_version": 1,
            "tool": {
                "name": "egress-plane",
                "version": "0.4.0",
                "data_digest": "sha256:" + "0" * 64,
                "runtime_range": {"python": ">=3.8", "node": ">=18", "rust": ">=1.70"},
            },
            "completeness": {
                "state": state,
                "budget_exceeded": list(budget),
                "coverage": {
                    "python": {"scanned": 1, "skipped": 0},
                    "javascript": {"scanned": 1, "skipped": 0},
                    "rust": {"scanned": 0, "skipped": 0},
                    "unsupported": {"scanned": 0, "skipped": 0},
                },
            },
            "caveats": [],
            "sites": list(sites),
            "blind_spots": list(blind_spots),
            "unscanned_dependencies": [],
            "excluded": [],
        }
    )


def egress_site(**kw):
    site = {
        "id": "a1b2c3d4e5f60708",
        "language": "python",
        "file": "src/client.py",
        "symbol": "fetch_cfg",
        "bucket": "primitive",
        "mechanism": "requests",
        "destination": {"kind": "env", "value": "API_URL", "display": "env(API_URL)"},
        "ordinal": 0,
        "labels": [],
        "location": {"line": 12, "column": 4},
    }
    site.update(kw)
    return site


def acl_site(**kw):
    site = {
        "site_id": "app.py::handler::0",
        "path": "app.py",
        "function": "handler",
        "framework": "flask",
        "method": "GET",
        "route": "/users/{id}",
        "id_params": [{"name": "id", "location": "path", "leaf": True}],
        "sink_kind": "sql",
        "rw": "read",
        "classes": ["authz"],
        "state": "deterministic",
        "test_path": False,
        "start_line": 9,
        "end_line": 20,
        "entry_start": 8,
        "entry_end": 20,
    }
    site.update(kw)
    return site


def acl_payload(findings=(), recognized=(), candidates=(), complete=True, verdict="findings present"):
    return json.dumps(
        {
            "schema_version": "acl-guard-audit/v1",
            "taxonomy_version": "v1",
            "ruleset_version": "0.3.0",
            "target": "/tree",
            "findings": list(findings),
            "recognized_complete": list(recognized),
            "candidates": list(candidates),
            "coverage": {
                "verdict": verdict,
                "complete": complete,
                "incomplete_reasons": [] if complete else ["Timeout: app.py"],
                "warnings": [] if complete else ["1 file(s) could not be parsed"],
                "entries": 3,
            },
        }
    )


# ---- egress adapter ----


class TestEgress:
    def test_maps_sites_to_outbound_records(self, tmp_path, root):
        exe = make_tool(
            tmp_path,
            "egress-plane",
            egress_payload(
                sites=[
                    egress_site(),
                    egress_site(
                        id="b2c3d4e5f6070809",
                        language="javascript",
                        file="web/app.ts",
                        symbol="init",
                        mechanism="fetch",
                        destination={
                            "kind": "literal",
                            "value": "api.example.com",
                            "display": "https://api.example.com",
                        },
                        ordinal=1,
                    ),
                    egress_site(
                        id="c3d4e5f607080910",
                        file="src/dns.py",
                        mechanism="dns",
                        destination={"kind": "param", "value": "host", "display": "host"},
                        location=None,
                        ordinal=2,
                    ),
                ]
            ),
        )
        res = egress.scan(root, str(exe))
        assert res.warnings == []
        assert res.complete
        by_id = {r.id: r for r in res.records}
        assert set(by_id) == {
            "requests:env:API_URL",
            "fetch:literal:api.example.com",
            "dns:param:host",
        }
        lit = by_id["fetch:literal:api.example.com"]
        assert lit.kind == "outbound"
        assert lit.lang == "ts"  # javascript folds into ts
        assert lit.file == "web/app.ts"
        assert lit.line == 12  # location.line passes through
        assert lit.extractor.startswith("egress-plane")
        no_loc = by_id["dns:param:host"]
        assert no_loc.line == 0  # null location omits the line
        assert all(r.kind == "outbound" for r in res.records)

    def test_exit2_incomplete_still_consumed(self, tmp_path, root):
        exe = make_tool(
            tmp_path,
            "egress-plane",
            egress_payload(sites=[egress_site()], state="incomplete", budget=["budget-files"]),
            code=2,
        )
        res = egress.scan(root, str(exe))
        assert [r.id for r in res.records] == ["requests:env:API_URL"]
        assert not res.complete
        assert res.records[0].attrs.get("low_confidence") is True
        assert any("incomplete" in n for n in res.gap_notes)
        assert any("budget-files" in n for n in res.gap_notes)

    def test_exit3_empty_is_valid(self, tmp_path, root):
        exe = make_tool(tmp_path, "egress-plane", egress_payload(state="empty"), code=3)
        res = egress.scan(root, str(exe))
        assert res.records == []
        assert res.complete  # a definitively empty scan is not partial coverage
        assert any("empty" in n for n in res.gap_notes)

    @pytest.mark.parametrize(
        "label", ["test", "vendored", "generated", "minified", "build-hook"]
    )
    def test_labeled_sites_dropped(self, tmp_path, root, label):
        exe = make_tool(
            tmp_path,
            "egress-plane",
            egress_payload(sites=[egress_site(labels=[label]), egress_site(id="d4e5f60708091011")]),
        )
        res = egress.scan(root, str(exe))
        assert [r.id for r in res.records] == ["requests:env:API_URL"]

    def test_blind_spots_become_gap_notes(self, tmp_path, root):
        spot = {
            "id": "e5f6070809101112",
            "language": "python",
            "file": "pkg/dynamic.py",
            "symbol": "load",
            "kind": "dynamic-import",
            "whole_file": False,
            "coverage_gap": True,
            "construct": "__import__(name)",
            "ordinal": 0,
            "labels": [],
            "location": {"line": 7, "column": 2},
        }
        exe = make_tool(tmp_path, "egress-plane", egress_payload(blind_spots=[spot]))
        res = egress.scan(root, str(exe))
        assert any("dynamic-import" in n and "pkg/dynamic.py:7" in n for n in res.gap_notes)

    def test_schema_version_mismatch_degrades(self, tmp_path, root):
        exe = make_tool(
            tmp_path, "egress-plane", egress_payload(sites=[egress_site()]).replace('"schema_version": 1', '"schema_version": 2')
        )
        res = egress.scan(root, str(exe))
        assert res.records == []
        assert not res.complete
        assert any("schema_version" in n for n in res.gap_notes)

    def test_unparseable_stdout_degrades(self, tmp_path, root):
        exe = make_tool(tmp_path, "egress-plane", "this is not json\n", code=1, stderr="boom")
        res = egress.scan(root, str(exe))
        assert res.records == []
        assert not res.complete
        assert res.warnings and res.gap_notes

    def test_missing_binary_degrades(self, root):
        res = egress.scan(root, None)
        assert res.records == []
        assert not res.complete
        assert res.warnings and res.gap_notes
        res = egress.scan(root, "/nonexistent/egress-plane")
        assert res.records == []
        assert not res.complete
        assert res.warnings


# ---- acl-guard-audit adapter ----


class TestAclGuard:
    def test_all_buckets_map_to_routes(self, tmp_path, root):
        exe = make_tool(
            tmp_path,
            "acl-guard-audit",
            acl_payload(
                findings=[acl_site()],
                recognized=[
                    acl_site(
                        site_id="api.ts::list::0",
                        path="api.ts",
                        function="list",
                        method=None,
                        route="/items",
                        entry_start=30,
                    )
                ],
                candidates=[
                    acl_site(
                        site_id="app.py::get::1",
                        path="app.py",
                        function="get",
                        method="POST",
                        route="/users/{id}/posts",
                        entry_start=40,
                        state="candidate",
                    )
                ],
            ),
        )
        res = aclguard.scan(root, str(exe))
        by_id = {r.id: r for r in res.records}
        assert set(by_id) == {"GET /users/{id}", "ANY /items", "POST /users/{id}/posts"}
        rec = by_id["GET /users/{id}"]
        assert rec.kind == "route"
        assert rec.file == "app.py"
        assert rec.line == 8  # entry_start
        assert rec.symbol == "handler"
        assert rec.lang == "python"
        assert by_id["ANY /items"].lang == "ts"
        assert res.complete

    def test_test_path_sites_dropped_no_route_is_gap_note(self, tmp_path, root):
        exe = make_tool(
            tmp_path,
            "acl-guard-audit",
            acl_payload(
                findings=[
                    acl_site(test_path=True),
                    acl_site(site_id="x::h::0", route=None, path="app.py", function="h"),
                ]
            ),
        )
        res = aclguard.scan(root, str(exe))
        assert res.records == []
        assert any("no route" in n for n in res.gap_notes)

    def test_exit2_with_json_still_consumed(self, tmp_path, root):
        exe = make_tool(
            tmp_path,
            "acl-guard-audit",
            acl_payload(findings=[acl_site()], complete=False, verdict="analysis incomplete"),
            code=2,
        )
        res = aclguard.scan(root, str(exe))
        assert [r.id for r in res.records] == ["GET /users/{id}"]
        assert not res.complete
        assert res.records[0].attrs.get("low_confidence") is True
        assert any("incomplete" in n for n in res.gap_notes)

    def test_schema_version_mismatch_degrades(self, tmp_path, root):
        exe = make_tool(
            tmp_path,
            "acl-guard-audit",
            acl_payload(findings=[acl_site()]).replace("acl-guard-audit/v1", "acl-guard-audit/v2"),
        )
        res = aclguard.scan(root, str(exe))
        assert res.records == []
        assert not res.complete
        assert any("schema_version" in n for n in res.gap_notes)

    def test_unparseable_and_missing_degrade(self, tmp_path, root):
        exe = make_tool(tmp_path, "acl-guard-audit", "semgrep exploded\n", code=2, stderr="err")
        res = aclguard.scan(root, str(exe))
        assert res.records == [] and not res.complete and res.warnings
        res = aclguard.scan(root, None)
        assert res.records == [] and not res.complete


# ---- helper runner ----


def go_record(**kw):
    rec = {
        "kind": "route",
        "id": "GET /v1/users",
        "file": "cmd/api/main.go",
        "line": 21,
        "symbol": "handleUsers",
        "lang": "go",
        "extractor": "go-extractor@0.1.0",
        "attrs": {},
    }
    rec.update(kw)
    return rec


class TestHelpers:
    def test_env_override_runs_helper(self, tmp_path, root, monkeypatch):
        monkeypatch.delenv("RSD_GO_EXTRACTOR", raising=False)
        exe = make_helper(
            tmp_path,
            "go-extractor",
            records=[go_record()],
            symbols=[{"file": "cmd/api/main.go", "symbol": "handleUsers", "start_line": 20, "end_line": 33}],
            edges=[{"caller": "main", "callee": "handleUsers"}],
        )
        monkeypatch.setenv("RSD_GO_EXTRACTOR", str(exe))
        res = helpers.run_helper("go", root)
        assert res.complete
        assert [r.id for r in res.records] == ["GET /v1/users"]
        assert res.symbols[0].symbol == "handleUsers"
        assert res.symbols[0].start_line == 20
        assert res.edges[0].caller == "main"

    def test_explicit_path_beats_env(self, tmp_path, root, monkeypatch):
        good = make_helper(tmp_path, "go-extractor", records=[go_record()])
        monkeypatch.setenv("RSD_GO_EXTRACTOR", "/nonexistent/nope")
        res = helpers.run_helper("go", root, explicit=str(good))
        assert [r.id for r in res.records] == ["GET /v1/users"]

    def test_nonzero_exit_flags_partial(self, tmp_path, root):
        exe = make_helper(tmp_path, "go-extractor", records=[go_record()], code=1)
        res = helpers.run_helper("go", root, explicit=str(exe))
        assert [r.id for r in res.records] == ["GET /v1/users"]
        assert not res.complete
        assert res.records[0].attrs.get("low_confidence") is True

    def test_missing_helper_degrades(self, root, monkeypatch, tmp_path):
        monkeypatch.delenv("RSD_RUST_EXTRACTOR", raising=False)
        # Point at a checkout with no helpers/ source so the helper is
        # genuinely absent regardless of which units have landed.
        monkeypatch.setattr(helpers, "_repo_root", lambda: tmp_path)
        res = helpers.run_helper("rust", root)
        assert res.records == []
        assert not res.complete
        assert res.warnings and res.gap_notes

    def test_garbage_stdout_degrades(self, tmp_path, root):
        exe = make_tool(tmp_path, "go-extractor", "not jsonl\n", code=0)
        res = helpers.run_helper("go", root, explicit=str(exe))
        assert res.records == []
        assert not res.complete

    def test_missing_sidecar_is_gap_note(self, tmp_path, root):
        # cat of a nonexistent sidecar file fails silently inside the script,
        # so no sidecar lands where the runner looks.
        script = tmp_path / "rust-extractor"
        payload = tmp_path / "rec.out"
        payload.write_text(json.dumps(go_record(lang="rust", file="src/lib.rs")) + "\n")
        script.write_text(f"#!/bin/sh\ncat {shlex.quote(str(payload))}\nexit 0\n")
        _chmod_x(script)
        res = helpers.run_helper("rust", root, explicit=str(script))
        assert len(res.records) == 1
        assert any("symbol" in n for n in res.gap_notes)

    @pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain absent")
    def test_ktd2_build_then_run(self, tmp_path, root, monkeypatch):
        """bin/ absent + sources present -> go build into bin/, then run."""
        fake_repo = tmp_path / "repo"
        src_dir = fake_repo / "helpers" / "go-extractor"
        src_dir.mkdir(parents=True)
        (src_dir / "go.mod").write_text("module example.com/fake\n\ngo 1.21\n")
        (src_dir / "main.go").write_text(
            """package main

import (
	"fmt"
	"os"
)

func main() {
	sym, edg := "", ""
	for i := 1; i < len(os.Args)-1; i++ {
		if os.Args[i] == "--symbols" {
			sym = os.Args[i+1]
		}
		if os.Args[i] == "--edges" {
			edg = os.Args[i+1]
		}
	}
	fmt.Println(`{"kind":"route","id":"GET /built","file":"main.go","line":3,"symbol":"h","lang":"go","extractor":"go-extractor@test","attrs":{}}`)
	if sym != "" {
		os.WriteFile(sym, []byte(`{"file":"main.go","symbol":"h","start_line":1,"end_line":9}`+"\\n"), 0644)
	}
	if edg != "" {
		os.WriteFile(edg, []byte(`{"caller":"main","callee":"h"}`+"\\n"), 0644)
	}
}
"""
        )
        monkeypatch.setattr(helpers, "_repo_root", lambda: fake_repo)
        res = helpers.run_helper("go", root)
        assert res.complete
        assert [r.id for r in res.records] == ["GET /built"]
        assert res.symbols[0].end_line == 9
        assert res.edges[0].callee == "h"
        binary = fake_repo / "bin" / "go-extractor"
        assert binary.is_file() and os.access(binary, os.X_OK)
        # second resolve reuses the binary without rebuilding
        path, why = helpers.resolve("go")
        assert path == binary and why is None


# ---- discovery (KTD3) ----


class TestDiscovery:
    def test_explicit_beats_env_beats_path(self, tmp_path, stub_path):
        on_path = make_tool(stub_path, "egress-plane", egress_payload(sites=[egress_site(file="via-path.py")]))
        via_env = make_tool(tmp_path, "env-tool", egress_payload(sites=[egress_site(file="via-env.py")]))
        explicit = make_tool(tmp_path, "flag-tool", egress_payload(sites=[egress_site(file="via-flag.py")]))
        os.environ["RSD_EGRESS_PLANE"] = str(via_env)
        path, warn = extract.find_tool("egress-plane", str(explicit), egress.ENV_VAR)
        assert warn is None and Path(path) == explicit
        path, warn = extract.find_tool("egress-plane", None, egress.ENV_VAR)
        assert warn is None and Path(path) == via_env
        del os.environ["RSD_EGRESS_PLANE"]
        path, warn = extract.find_tool("egress-plane", None, egress.ENV_VAR)
        assert warn is None and Path(path) == on_path
        os.environ.pop("RSD_EGRESS_PLANE", None)

    def test_missing_tool_warns(self, stub_path):
        path, warn = extract.find_tool("egress-plane", None, egress.ENV_VAR)
        assert path is None and "egress-plane" in warn

    def test_bad_explicit_path_warns(self, stub_path):
        path, warn = extract.find_tool("egress-plane", "/nonexistent/tool", egress.ENV_VAR)
        assert path is None and "/nonexistent/tool" in warn

    def test_bad_env_path_warns(self, stub_path, monkeypatch):
        monkeypatch.setenv("RSD_EGRESS_PLANE", "/nonexistent/env-tool")
        path, warn = extract.find_tool("egress-plane", None, egress.ENV_VAR)
        assert path is None and "/nonexistent/env-tool" in warn


# ---- extract dispatch ----


class TestExtractTree:
    def test_python_ts_dispatch_runs_both_adapters(self, stub_path, root):
        make_tool(stub_path, "egress-plane", egress_payload(sites=[egress_site()]))
        make_tool(stub_path, "acl-guard-audit", acl_payload(findings=[acl_site()]))
        res = extract.extract_tree(root, ("python", "ts"))
        ids = {r.id for r in res.records}
        assert "requests:env:API_URL" in ids
        assert "GET /users/{id}" in ids
        # in-repo route extractor (U5) runs alongside; no absence note (KD1)
        assert not any("not present" in n for n in res.gap_notes)

    def test_records_outside_requested_langs_dropped(self, stub_path, root):
        make_tool(
            stub_path,
            "egress-plane",
            egress_payload(
                sites=[
                    egress_site(),
                    egress_site(
                        id="f607080910111213",
                        language="javascript",
                        file="web/app.ts",
                        ordinal=1,
                    ),
                ]
            ),
        )
        res = extract.extract_tree(root, ("python",))
        assert all(r.lang == "python" for r in res.records)
        assert [r.id for r in res.records] == ["requests:env:API_URL"]

    def test_go_dispatch_uses_helper_and_egress(self, stub_path, root, tmp_path, monkeypatch):
        make_tool(stub_path, "egress-plane", egress_payload(sites=[egress_site(language="go", file="net/out.go")]))
        exe = make_helper(tmp_path, "go-extractor", records=[go_record()])
        monkeypatch.setenv("RSD_GO_EXTRACTOR", str(exe))
        res = extract.extract_tree(root, ("go",))
        ids = {r.id for r in res.records}
        assert "GET /v1/users" in ids
        assert "requests:env:API_URL" in ids

    def test_absent_tool_names_a_gap(self, stub_path, root, tmp_path, monkeypatch):
        # nothing on PATH for go: egress, acl, and the helper all degrade.
        # _repo_root is isolated so an absent helper stays absent even when
        # helper sources exist in the real checkout.
        monkeypatch.setattr(helpers, "_repo_root", lambda: tmp_path)
        res = extract.extract_tree(root, ("go",))
        assert res.records == []
        assert not res.complete
        assert any("egress-plane" in n for n in res.gap_notes)
        assert any("go-extractor" in n for n in res.gap_notes)

    def test_path_matcher_filters_posthoc(self, stub_path, root):
        make_tool(
            stub_path,
            "egress-plane",
            egress_payload(
                sites=[
                    egress_site(file="tests/test_client.py"),
                    egress_site(id="0809101112131415", file="src/client.py", ordinal=1),
                ]
            ),
        )
        res = extract.extract_tree(root, ("python",), matcher=PathMatcher())
        assert [r.file for r in res.records] == ["src/client.py"]

    def test_extract_lang_single(self, stub_path, root):
        make_tool(stub_path, "egress-plane", egress_payload(sites=[egress_site()]))
        res = extract.extract_lang(root, "python")
        assert [r.id for r in res.records] == ["requests:env:API_URL"]


# ---- CLI wiring ----


class TestCliExtract:
    def test_extract_command_prints_records(self, stub_path, root, capsys):
        make_tool(stub_path, "egress-plane", egress_payload(sites=[egress_site()]))
        make_tool(stub_path, "acl-guard-audit", acl_payload(findings=[acl_site()]))
        code = cli.main(["extract", "--root", str(root), "--lang", "python"])
        assert code == cli.EXIT_NO_DELTA
        out = capsys.readouterr()
        lines = [json.loads(l) for l in out.out.splitlines() if l.strip()]
        ids = {l["id"] for l in lines}
        assert "requests:env:API_URL" in ids
        assert "GET /users/{id}" in ids
        assert "not present" not in out.err  # U5 landed: no absence note
