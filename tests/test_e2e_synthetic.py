"""U10: end-to-end validation on the synthetic diff-repo fixture.

The fixture (tests/fixtures/diff-repo/build_repo.py) is the spec's
synthetic two-commit repo, python-only so the in-repo py_ts_routes
extractor covers it with no external tools:

- v1.0.0 -> v1.1.0: one added route, one removed CLI flag (observable as
  a removed auth hunk: python emits no cli_flag records), one renamed
  handler, one removed bounds check. surface.json and guards.json must
  match the committed goldens exactly; exit code 1.
- v1.1.0 -> v1.1.1 tags the same commit: no delta, exit 0.
- v1.0.0 -> v1.0.0 is an identical pair: tool error, exit 2.
- v1.1.0 -> v1.2.0 changes only guard code: a guard-only delta, exit 1.

egress-plane and acl-guard-audit are absent in this environment, so the
delta records carry attrs.low_confidence; the goldens were generated
under the same condition.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from release_surface_diff import cli
from release_surface_diff.guards import CLASSES

FIXTURES = Path(__file__).parent / "fixtures" / "diff-repo"
GOLDEN_SURFACE = FIXTURES / "golden_surface.json"
GOLDEN_GUARDS = FIXTURES / "golden_guards.json"

SEED_FIELDS = {
    "repo", "sha", "parent", "source", "class", "files", "symbol", "evidence",
}
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "rsd_diff_repo_builder", FIXTURES / "build_repo.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def synthetic_repo(tmp_path_factory) -> Path:
    """The built fixture repo; basename 'synthetic' fixes the output slug."""
    builder = _load_builder()
    base = tmp_path_factory.mktemp("diff-repo")
    return builder.build(base / "synthetic")


def _run(repo: Path, out: Path, from_tag: str, to_tag: str,
         *extra: str) -> int:
    return cli.main([
        "run", "--repo", str(repo),
        "--from", from_tag, "--to", to_tag,
        "--out", str(out), *extra,
    ])


def _out_dir(out: Path, from_tag: str, to_tag: str) -> Path:
    return out / "synthetic" / f"{from_tag}..{to_tag}"


def _load_json(path: Path) -> dict:
    doc = json.loads(path.read_text())
    assert isinstance(doc.get("generated_at"), str) and doc["generated_at"]
    del doc["generated_at"]
    return doc


def _golden(path: Path) -> dict:
    doc = json.loads(path.read_text())
    doc.pop("generated_at", None)
    return doc


def _normalize_volatile(text: str) -> str:
    """Blank the one intentionally non-deterministic field (R16)."""
    text = re.sub(r'"generated_at": "[^"]*"', '"generated_at": "<ts>"', text)
    return re.sub(r"^generated: .*$", "generated: <ts>", text, flags=re.M)


class TestSyntheticE2E:
    def test_golden_surface_guards_and_seeds(self, synthetic_repo, tmp_path):
        out = tmp_path / "out"
        code = _run(synthetic_repo, out, "v1.0.0", "v1.1.0", "--lang", "python")
        assert code == cli.EXIT_DELTA

        pair = _out_dir(out, "v1.0.0", "v1.1.0")
        for name in ("surface.json", "guards.json", "report.md", "seeds.jsonl"):
            assert (pair / name).is_file(), name

        surface = _load_json(pair / "surface.json")
        assert surface == _golden(GOLDEN_SURFACE)
        added_ids = {r["id"] for r in surface["added"]}
        assert added_ids == {"POST /admin/rotate-keys"}
        rename = surface["renamed"][0]
        assert rename["before"]["id"] == "GET /users/{id}"
        assert rename["after"]["id"] == "GET /members/{id}"

        guards = _load_json(pair / "guards.json")
        assert guards == _golden(GOLDEN_GUARDS)
        by_file = {h["file"]: h for h in guards["hunks"]}
        assert by_file["app.py"]["class"] == "bounds"
        assert by_file["app.py"]["direction"] == "removed"
        assert by_file["app.py"]["symbol"] == "decode_payload"
        # The removed --skip-verify flag lands as a removed auth hunk.
        assert by_file["cli.py"]["class"] == "auth"
        assert by_file["cli.py"]["direction"] == "removed"

        seeds = [
            json.loads(line)
            for line in (pair / "seeds.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert seeds, "one seed per guard hunk"
        assert len(seeds) == len(guards["hunks"])
        for seed in seeds:
            assert set(seed) == SEED_FIELDS
            assert seed["repo"] == "synthetic"
            assert seed["source"] == "release-diff"
            assert seed["class"] in CLASSES
            assert isinstance(seed["files"], list) and seed["files"]
            assert _SHA_RE.match(seed["sha"])
            assert seed["parent"] is None or _SHA_RE.match(seed["parent"])

        report = (pair / "report.md").read_text()
        assert "## SCOPE" in report and "## Coverage" in report
        # The renamed pair renders in the entry-point delta section.
        assert "GET /users/{id}` -> `GET /members/{id}" in report

    def test_deterministic_rerun(self, synthetic_repo, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        _run(synthetic_repo, a, "v1.0.0", "v1.1.0", "--lang", "python")
        _run(synthetic_repo, b, "v1.0.0", "v1.1.0", "--lang", "python")
        pa = _out_dir(a, "v1.0.0", "v1.1.0")
        pb = _out_dir(b, "v1.0.0", "v1.1.0")
        for name in ("surface.json", "guards.json", "report.md", "seeds.jsonl"):
            ta = _normalize_volatile((pa / name).read_text())
            tb = _normalize_volatile((pb / name).read_text())
            assert ta == tb, name

    def test_autodetect_selects_python(self, synthetic_repo, tmp_path):
        # No --lang: the pyproject.toml manifest drives autodetect (R1).
        out = tmp_path / "out"
        code = _run(synthetic_repo, out, "v1.0.0", "v1.1.0")
        assert code == cli.EXIT_DELTA
        surface = _load_json(
            _out_dir(out, "v1.0.0", "v1.1.0") / "surface.json"
        )
        assert all(r["lang"] == "python" for r in surface["added"])

    def test_same_tag_exits_2(self, synthetic_repo, tmp_path):
        code = _run(synthetic_repo, tmp_path / "out", "v1.0.0", "v1.0.0")
        assert code == cli.EXIT_ERROR

    def test_no_delta_exits_0(self, synthetic_repo, tmp_path):
        # v1.1.1 tags the same commit as v1.1.0: the pair diffs to nothing.
        code = _run(synthetic_repo, tmp_path / "out", "v1.1.0", "v1.1.1")
        assert code == cli.EXIT_NO_DELTA

    def test_guard_only_delta_exits_1(self, synthetic_repo, tmp_path):
        # v1.2.0 adds a bounds check with no surface change: R4 still
        # wants exit 1 for a guard-only delta.
        out = tmp_path / "out"
        code = _run(synthetic_repo, out, "v1.1.0", "v1.2.0", "--lang", "python")
        assert code == cli.EXIT_DELTA
        surface = _load_json(
            _out_dir(out, "v1.1.0", "v1.2.0") / "surface.json"
        )
        assert not any(
            surface[k] for k in ("added", "removed", "changed", "renamed")
        )
        guards = _load_json(
            _out_dir(out, "v1.1.0", "v1.2.0") / "guards.json"
        )
        assert guards["hunks"]
        assert all(h["direction"] == "added" for h in guards["hunks"])
