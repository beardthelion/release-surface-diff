"""U5 tests: bare-literal protocol-id pass (R10).

protocol_ids.py is the single producer for req/resp protocol strings and
gossip topic templates in every language; the helper binaries only emit
structured registrations, so nothing here may double-emit or miss the
/eth2/...-style literals.
"""

from pathlib import Path

import pytest

from release_surface_diff.extractors import protocol_ids


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _ids(result):
    return {r.id for r in result.records}


class TestProtocolLiterals:
    def test_req_resp_and_gossip_across_langs(self, tmp_path):
        _write(
            tmp_path,
            "p2p/protocols.rs",
            'pub const STATUS: &str = "/eth2/beacon_chain/req/status/1/ssz_snappy";\n'
            'pub const BLOCKS: &str = "/eth2/beacon_chain/req/beacon_blocks_by_range/2/ssz_snappy";\n',
        )
        _write(
            tmp_path,
            "p2p/gossip.go",
            'const topic = "/eth2/%x/beacon_block/ssz_snappy"\n'
            'const sub = "/meshsub/1.1.0"\n',
        )
        _write(
            tmp_path,
            "net/ids.py",
            'PROTO = "/ipfs/id/1.0.0"\n'
            'TOPIC = "beacon_attestation_%d"\n',
        )
        res = protocol_ids.extract(tmp_path)
        ids = _ids(res)
        assert {
            "/eth2/beacon_chain/req/status/1/ssz_snappy",
            "/eth2/beacon_chain/req/beacon_blocks_by_range/2/ssz_snappy",
            "/eth2/%x/beacon_block/ssz_snappy",
            "/meshsub/1.1.0",
            "/ipfs/id/1.0.0",
            "beacon_attestation_%d",
        } == ids

    def test_record_shape_and_lang(self, tmp_path):
        _write(
            tmp_path,
            "p2p/proto.rs",
            'pub fn protocols() {\n'
            '    let p = "/eth2/beacon_chain/req/ping/1/ssz_snappy";\n'
            '}\n',
        )
        res = protocol_ids.extract(tmp_path)
        (rec,) = res.records
        assert rec.kind == "protocol_id"
        assert rec.id == "/eth2/beacon_chain/req/ping/1/ssz_snappy"
        assert rec.file == "p2p/proto.rs"
        assert rec.line == 2
        assert rec.symbol == "protocols"
        assert rec.lang == "rust"
        assert rec.extractor == protocol_ids.EXTRACTOR

    def test_url_paths_and_ordinary_strings_ignored(self, tmp_path):
        _write(
            tmp_path,
            "web/routes.ts",
            'app.get("/users/{id}", h);\n'
            'const base = "/api/v1/users";\n'
            'const url = "https://example.com/x/y";\n'
            'const verb = "GET";\n'
            'const plain = "hello world";\n',
        )
        res = protocol_ids.extract(tmp_path)
        assert res.records == []

    def test_same_literal_dedupes_per_file(self, tmp_path):
        _write(
            tmp_path,
            "p2p/a.go",
            'const a = "/meshsub/1.0.0"\nconst b = "/meshsub/1.0.0"\n',
        )
        _write(tmp_path, "p2p/b.go", 'const c = "/meshsub/1.0.0"\n')
        res = protocol_ids.extract(tmp_path)
        matches = [r for r in res.records if r.id == "/meshsub/1.0.0"]
        # one record per file carrying the literal
        assert sorted((r.file, r.line) for r in matches) == [("p2p/a.go", 1), ("p2p/b.go", 1)]

    def test_excluded_paths_skipped(self, tmp_path):
        _write(tmp_path, "vendor/x.go", 'const p = "/meshsub/1.0.0"\n')
        _write(tmp_path, "docs/spec.md", '"/eth2/beacon_chain/req/status/1/ssz_snappy"\n')
        res = protocol_ids.extract(tmp_path)
        assert res.records == []

    def test_unsupported_extensions_skipped(self, tmp_path):
        _write(tmp_path, "notes.txt", '"/meshsub/1.0.0"\n')
        _write(tmp_path, "schema.proto", '"/meshsub/1.0.0"\n')
        res = protocol_ids.extract(tmp_path)
        assert res.records == []

    def test_integration_wired_in_extract_tree(self, tmp_path, monkeypatch):
        """extract_tree merges the pass for any requested lang."""
        from release_surface_diff import extract

        monkeypatch.delenv("RSD_EGRESS_PLANE", raising=False)
        monkeypatch.delenv("RSD_GO_EXTRACTOR", raising=False)
        _write(tmp_path, "p2p/proto.go", 'const p = "/meshsub/1.1.0"\n')
        res = extract.extract_tree(tmp_path, ("go",))
        assert "/meshsub/1.1.0" in _ids(res)
