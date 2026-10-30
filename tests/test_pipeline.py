"""U7: end-to-end `run` pipeline on a synthetic two-tag repo.

The repo has one python file so no external extractors are needed: the
in-repo py_ts_routes extractor covers it. v1.0.0 -> v1.1.0 adds a route
and removes a bounds check, so all four outputs carry a delta.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from release_surface_diff import cli


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "config", "tag.gpgsign", "false")
    return path


def commit(repo: Path, *names: str, msg: str) -> None:
    git(repo, "add", *names)
    git(repo, "commit", "-qm", msg)


V1 = '''from flask import Flask

app = Flask(__name__)


@app.get("/health")
def health():
    return "ok"


@app.post("/decode")
def decode():
    data = request.get_data()
    if len(data) > MAX_BODY:
        abort(413)
    return parse(data)
'''

V2 = '''from flask import Flask

app = Flask(__name__)


@app.get("/health")
def health():
    return "ok"


@app.post("/decode")
def decode():
    data = request.get_data()
    return parse(data)


@app.post("/admin/reset")
def reset():
    return do_reset()
'''


@pytest.fixture
def two_tag_repo(tmp_path: Path) -> Path:
    repo = init_repo(tmp_path / "target")
    (repo / "app.py").write_text(V1)
    commit(repo, "app.py", msg="v1")
    git(repo, "tag", "v1.0.0")
    (repo / "app.py").write_text(V2)
    commit(repo, "app.py", msg="v2")
    git(repo, "tag", "v1.1.0")
    return repo


def run(repo: Path, out: Path, *extra: str) -> int:
    argv = ["run", "--repo", str(repo), "--from", "v1.0.0", "--to", "v1.1.0",
            "--out", str(out), "--lang", "python", *extra]
    return cli.main(argv)


class TestRunPipeline:
    def test_all_four_outputs_and_delta(self, two_tag_repo, tmp_path):
        out = tmp_path / "out"
        assert run(two_tag_repo, out) == cli.EXIT_DELTA
        pair = out / "target" / "v1.0.0..v1.1.0"
        for name in ("surface.json", "guards.json", "report.md", "seeds.jsonl"):
            assert (pair / name).exists(), name

        surface = json.loads((pair / "surface.json").read_text())
        added_ids = {r["id"] for r in surface["added"]}
        assert "POST /admin/reset" in added_ids
        assert surface["from"] == "v1.0.0"
        assert surface["to"] == "v1.1.0"

        guards = json.loads((pair / "guards.json").read_text())
        assert guards["hunks"], "removed bounds check should classify"
        bounds = [h for h in guards["hunks"] if h["class"] == "bounds"]
        assert bounds and bounds[0]["direction"] == "removed"

        seeds = [
            json.loads(l)
            for l in (pair / "seeds.jsonl").read_text().splitlines()
            if l.strip()
        ]
        assert len(seeds) == len(guards["hunks"])
        assert all(s["source"] == "release-diff" for s in seeds)

        report = (pair / "report.md").read_text()
        assert "## SCOPE" in report
        assert "POST /admin/reset" in report
        assert "## Coverage" in report

    def test_no_delta_exits_zero(self, two_tag_repo, tmp_path):
        # v1.2.0 tags the same commit as v1.1.0: the pair diffs to nothing.
        git(two_tag_repo, "tag", "v1.2.0", "v1.1.0")
        out = tmp_path / "out"
        assert (
            cli.main([
                "run", "--repo", str(two_tag_repo),
                "--from", "v1.1.0", "--to", "v1.2.0",
                "--out", str(out), "--lang", "python",
            ]) == cli.EXIT_NO_DELTA
        )

    def test_deterministic_outputs(self, two_tag_repo, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        run(two_tag_repo, a)
        run(two_tag_repo, b)
        for name in ("surface.json", "guards.json", "seeds.jsonl", "report.md"):
            pa = next(a.rglob(name)).read_text()
            pb = next(b.rglob(name)).read_text()
            # generated_at is the one intentional non-determinism (R16)
            if name in ("surface.json", "guards.json", "report.md"):
                import re
                pa = re.sub(r'"generated_at": "[^"]*"', '"G"', pa)
                pb = re.sub(r'"generated_at": "[^"]*"', '"G"', pb)
                pa = re.sub(r"generated: [^\n]*", "G", pa)
                pb = re.sub(r"generated: [^\n]*", "G", pb)
            assert pa == pb, name

    def test_watch_still_errors(self, tmp_path):
        cfg = tmp_path / "targets.toml"
        cfg.write_text('[[target]]\nrepo = "/nonexistent"\n')
        assert cli.main(["watch", "--config", str(cfg)]) == cli.EXIT_ERROR
