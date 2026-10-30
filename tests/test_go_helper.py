"""U4 integration: the real go-extractor binary through adapters/helpers.py.

The runner contract is `<bin> <root> --symbols <path> --edges <path>` with
record JSONL on stdout; this test exercises it end to end against the
shared Go fixture, building bin/go-extractor via the KTD2 resolve path.
"""

import shutil
from pathlib import Path

import pytest

sys_path_root = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(sys_path_root / "src"))

from release_surface_diff.adapters import helpers
from release_surface_diff.records import Record

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "go"

pytestmark = pytest.mark.skipif(
    shutil.which("go") is None, reason="go toolchain absent"
)


@pytest.fixture(scope="module")
def result():
    # resolve() builds helpers/go-extractor into bin/ on first use (KTD2).
    return helpers.run_helper("go", FIXTURE)


def test_binary_resolves_and_runs(result):
    assert result.complete, f"helper degraded: {result.gap_notes}"
    assert result.records, "no records produced"
    assert all(isinstance(r, Record) for r in result.records)
    assert all(r.lang == "go" for r in result.records)


def test_surface_records_round_trip(result):
    ids = {(r.kind, r.id) for r in result.records}
    expected = {
        ("route", "GET /users/{id}"),
        ("route", "POST /api/login"),
        ("route", "PUT /items/{id}"),
        ("route", "ANY /hello"),
        ("route", "GET /status"),
        ("rpc", "user.User"),
        ("rpc", "eth"),
        ("cli_flag", "--timeout"),
        ("cli_flag", "--config"),
        ("config_key", "server.port"),
        ("export", "example.com/surface/lib.Client.Connect"),
    }
    missing = expected - ids
    assert not missing, f"missing records: {missing}"
    chi = next(r for r in result.records if r.id == "GET /users/{id}")
    assert chi.symbol == "getUser"
    assert chi.attrs.get("auth_hint") == "authMw"
    assert chi.line > 0


def test_sidecars_round_trip(result):
    spans = {(s.file, s.symbol) for s in result.symbols}
    assert ("surface/server.go", "getUser") in spans
    assert ("surface/lib/lib.go", "Client.Connect") in spans
    edges = {(e.caller, e.callee) for e in result.edges}
    assert ("main", "auth") in edges
    assert ("getUser", "loadUser") in edges


def test_exclusions_round_trip(result):
    for rec in result.records:
        assert ".pb.go" not in rec.file
        assert "broken.go" not in rec.file
        if rec.kind == "export":
            assert "internal" not in rec.file
