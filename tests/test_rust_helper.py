"""U8 integration: the real rust-extractor binary through adapters/helpers.py.

The runner contract is `<bin> <root> --symbols <path> --edges <path>` with
record JSONL on stdout; this test exercises it end to end against the
shared Rust fixture, building bin/rust-extractor via the KTD2 resolve
path (cargo build --release).
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

sys_path_root = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(sys_path_root / "src"))

from release_surface_diff.adapters import helpers
from release_surface_diff.records import Record

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "rust"
GOLDEN = FIXTURE / "golden" / "records.jsonl"

pytestmark = pytest.mark.skipif(
    shutil.which("cargo") is None, reason="cargo toolchain absent"
)


@pytest.fixture(scope="module")
def result():
    # resolve() builds helpers/rust-extractor into bin/ on first use (KTD2).
    return helpers.run_helper("rust", FIXTURE)


def test_binary_resolves_and_runs(result):
    assert result.complete, f"helper degraded: {result.gap_notes}"
    assert result.records, "no records produced"
    assert all(isinstance(r, Record) for r in result.records)
    assert all(r.lang == "rust" for r in result.records)


def test_surface_records_round_trip(result):
    ids = {(r.kind, r.id) for r in result.records}
    expected = {
        ("route", "GET /health"),
        ("route", "GET /api/users/{id}"),  # axum .nest prefix joined
        ("route", "ANY /static"),
        ("route", "GET /users/{id}"),  # actix #[get] attr route
        ("route", "PUT /api/v1/things"),  # actix web::scope prefix
        ("route", "GET /users/{param}"),  # warp path::param
        ("route", "GET /api/v1/things/{param}"),  # warp path! macro
        ("rpc", "eth_getProof"),  # jsonrpsee #[method(name=)]
        ("rpc", "eth_block_number"),  # jsonrpsee implicit namespace name
        ("rpc", "Greeter"),  # tonic add_service(XServer::new)
        ("rpc", "debug_traceCall"),  # register_method literal
        ("cli_flag", "--verbose"),
        ("cli_flag", "--output-dir"),
        ("cli_flag", "--diff-report"),  # subcommand rename honored
        ("config_key", "http-port"),  # serde rename honored
        ("config_key", "p2p.max-peers"),  # nested struct dotted path
        ("export", "server::serve"),
        ("export", "Engine::start"),
        ("export", "Node"),  # pub use re-export alias
        ("protocol_id", "BEACON_STATUS"),  # non-literal registration only
        ("protocol_id", "/eth2/beacon_chain/req/blocks/1/ssz_snappy"),  # concat!
    }
    missing = expected - ids
    assert not missing, f"missing records: {missing}"
    # bare literal protocol args are orchestrator-owned: absent here
    assert ("protocol_id", "/eth2/beacon_chain/req/status/1/ssz_snappy") not in ids
    # pub(crate) and private items are not exports
    export_ids = {r.id for r in result.records if r.kind == "export"}
    assert "internals::helper" not in export_ids
    assert "server::internal_serve" not in export_ids
    assert "crate_only" not in export_ids


def test_golden_records_match(result):
    golden = [json.loads(l) for l in GOLDEN.read_text().splitlines() if l.strip()]
    got = sorted(
        (r.to_dict() for r in result.records),
        key=lambda d: (d["kind"], d["id"], d["file"], d["line"], d["symbol"]),
    )
    want = sorted(
        golden,
        key=lambda d: (d["kind"], d["id"], d["file"], d["line"], d["symbol"]),
    )
    assert got == want


def test_sidecars_round_trip(result):
    spans = {(s.file, s.symbol) for s in result.symbols}
    assert ("src/api.rs", "server::serve") in spans
    assert ("src/api.rs", "Engine::start") in spans
    assert ("src/api.rs", "server::hidden::secret") in spans  # full inventory
    edges = {(e.caller, e.callee) for e in result.edges}
    assert ("main_entry", "helper_call") in edges
    assert ("main_entry", "server::serve") in edges


def test_broken_file_degrades_not_crashes(result):
    # src/broken.rs fails syn::parse_file; the run still completes and no
    # record claims to come from it
    assert result.complete
    assert all("broken.rs" not in r.file for r in result.records)
