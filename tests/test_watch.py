"""U9: `watch` checks targets.toml for new tags, runs each pair, and keeps
state.json next to the config (KTD9).

Most tests inject a fake runner (recording calls, returning a canned
(exit, degraded)); the cli-level tests drive the real run pipeline on a
small flask repo.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from release_surface_diff import cli, config, watch


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "config", "tag.gpgsign", "false")
    return path


def commit_file(repo: Path, name: str, content: str, message: str) -> None:
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-qm", message)


def tag(repo: Path, name: str) -> None:
    git(repo, "tag", name)


@pytest.fixture
def tagged_repo(tmp_path: Path) -> Path:
    repo = init_repo(tmp_path / "target")
    commit_file(repo, "app.txt", "v1.0\n", "first")
    tag(repo, "v1.0.0")
    commit_file(repo, "app.txt", "v1.1\n", "second")
    tag(repo, "v1.1.0")
    return repo


def write_config(tmp_path: Path, repo, extra: str = "") -> Path:
    cfg = tmp_path / "targets.toml"
    cfg.write_text(
        f'[[target]]\nrepo = "{repo}"\nlang = ["python"]\n{extra}'
    )
    return cfg


def seed_state(tmp_path: Path, repo, last_tag: str) -> None:
    (tmp_path / "state.json").write_text(
        json.dumps({str(repo): {"last_tag": last_tag}})
    )


def read_state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "state.json").read_text())


class FakeRunner:
    """cli.run_pair stand-in: records (from, to) pairs and returns a
    default (exit, degraded), overridable per pair."""

    def __init__(self, result=(0, False), results=None):
        self.calls = []
        self.result = result
        self.results = results or {}

    def __call__(self, repo_arg, repo_path, from_tag, to_tag, **kwargs):
        self.calls.append((from_tag, to_tag))
        return self.results.get((from_tag, to_tag), self.result)


# ---- new-tag detection and ordering ----


def test_first_run_processes_newest_pair_and_advances(tagged_repo, tmp_path):
    cfg = write_config(tmp_path, tagged_repo)
    runner = FakeRunner()
    assert watch.check(cfg, runner=runner) == 0
    assert runner.calls == [("v1.0.0", "v1.1.0")]
    entry = read_state(tmp_path)[str(tagged_repo)]
    assert entry["last_tag"] == "v1.1.0"
    assert entry["last_run_exit"] == 0
    assert entry["degraded"] is False
    assert entry["last_checked"]


def test_second_run_no_new_tag_does_no_work(tagged_repo, tmp_path):
    cfg = write_config(tmp_path, tagged_repo)
    runner = FakeRunner()
    assert watch.check(cfg, runner=runner) == 0
    runner.calls.clear()
    assert watch.check(cfg, runner=runner) == 0
    assert runner.calls == []
    assert read_state(tmp_path)[str(tagged_repo)]["last_tag"] == "v1.1.0"


def test_two_new_tags_run_each_pair_in_semver_order(tagged_repo, tmp_path):
    commit_file(tagged_repo, "app.txt", "v1.2\n", "third")
    tag(tagged_repo, "v1.2.0")
    cfg = write_config(tmp_path, tagged_repo)
    seed_state(tmp_path, tagged_repo, "v1.0.0")
    runner = FakeRunner()
    assert watch.check(cfg, runner=runner) == 0
    assert runner.calls == [("v1.0.0", "v1.1.0"), ("v1.1.0", "v1.2.0")]
    assert read_state(tmp_path)[str(tagged_repo)]["last_tag"] == "v1.2.0"


def test_backport_tag_orders_by_semver_not_discovery(tmp_path):
    repo = init_repo(tmp_path / "target")
    commit_file(repo, "app.txt", "a\n", "one")
    tag(repo, "v1.4.1")
    commit_file(repo, "app.txt", "b\n", "two")
    tag(repo, "v1.5.0")
    commit_file(repo, "app.txt", "c\n", "three")
    tag(repo, "v1.4.2")  # backport cut after v1.5.0
    cfg = write_config(tmp_path, repo)
    seed_state(tmp_path, repo, "v1.4.1")
    runner = FakeRunner()
    assert watch.check(cfg, runner=runner) == 0
    assert runner.calls == [("v1.4.1", "v1.4.2"), ("v1.4.2", "v1.5.0")]
    assert read_state(tmp_path)[str(repo)]["last_tag"] == "v1.5.0"


def test_tag_order_is_semver_not_lexicographic(tmp_path):
    # "v1.10.0" < "v1.9.0" lexicographically, so list order cannot be trusted.
    repo = init_repo(tmp_path / "target")
    commit_file(repo, "app.txt", "1\n", "one")
    tag(repo, "v1.9.0")
    commit_file(repo, "app.txt", "2\n", "two")
    tag(repo, "v1.10.0")
    cfg = write_config(tmp_path, repo)
    runner = FakeRunner()
    assert watch.check(cfg, runner=runner) == 0
    assert runner.calls == [("v1.9.0", "v1.10.0")]
    assert read_state(tmp_path)[str(repo)]["last_tag"] == "v1.10.0"


def test_no_newer_tag_when_backport_is_older_than_state(tmp_path):
    repo = init_repo(tmp_path / "target")
    commit_file(repo, "app.txt", "a\n", "one")
    tag(repo, "v1.4.1")
    commit_file(repo, "app.txt", "b\n", "two")
    tag(repo, "v1.5.0")
    commit_file(repo, "app.txt", "c\n", "three")
    tag(repo, "v1.4.9")  # backport older than the recorded release
    cfg = write_config(tmp_path, repo)
    seed_state(tmp_path, repo, "v1.5.0")
    runner = FakeRunner()
    assert watch.check(cfg, runner=runner) == 0
    assert runner.calls == []


# ---- failure and degradation (KTD9) ----


def test_failed_run_records_outcome_without_advancing(tagged_repo, tmp_path):
    cfg = write_config(tmp_path, tagged_repo)
    runner = FakeRunner(result=(2, False))
    assert watch.check(cfg, runner=runner) == 2
    entry = read_state(tmp_path)[str(tagged_repo)]
    assert entry["last_run_exit"] == 2
    assert "last_tag" not in entry
    # the pair retries on the next invocation
    retry = FakeRunner()
    assert watch.check(cfg, runner=retry) == 0
    assert retry.calls == [("v1.0.0", "v1.1.0")]
    assert read_state(tmp_path)[str(tagged_repo)]["last_tag"] == "v1.1.0"


def test_failed_run_stops_later_pending_tags(tagged_repo, tmp_path):
    commit_file(tagged_repo, "app.txt", "v1.2\n", "third")
    tag(tagged_repo, "v1.2.0")
    cfg = write_config(tmp_path, tagged_repo)
    seed_state(tmp_path, tagged_repo, "v1.0.0")
    runner = FakeRunner(result=(2, False))
    assert watch.check(cfg, runner=runner) == 2
    assert runner.calls == [("v1.0.0", "v1.1.0")]  # v1.2.0 left for next time
    entry = read_state(tmp_path)[str(tagged_repo)]
    assert entry["last_tag"] == "v1.0.0"


def test_degraded_run_records_without_advancing(tagged_repo, tmp_path):
    cfg = write_config(tmp_path, tagged_repo)
    runner = FakeRunner(result=(1, True))
    assert watch.check(cfg, runner=runner) == 0
    entry = read_state(tmp_path)[str(tagged_repo)]
    assert entry["last_run_exit"] == 1
    assert entry["degraded"] is True
    assert "last_tag" not in entry


def test_degraded_run_retries_pair_next_invocation(tagged_repo, tmp_path):
    commit_file(tagged_repo, "app.txt", "v1.2\n", "third")
    tag(tagged_repo, "v1.2.0")
    cfg = write_config(tmp_path, tagged_repo)
    seed_state(tmp_path, tagged_repo, "v1.0.0")
    runner = FakeRunner(results={("v1.0.0", "v1.1.0"): (0, True)})
    assert watch.check(cfg, runner=runner) == 0
    assert runner.calls == [("v1.0.0", "v1.1.0")]
    assert read_state(tmp_path)[str(tagged_repo)]["last_tag"] == "v1.0.0"
    retry = FakeRunner()
    assert watch.check(cfg, runner=retry) == 0
    assert retry.calls == [("v1.0.0", "v1.1.0"), ("v1.1.0", "v1.2.0")]
    assert read_state(tmp_path)[str(tagged_repo)]["last_tag"] == "v1.2.0"


def test_runner_exception_counts_as_failed_run(tagged_repo, tmp_path):
    cfg = write_config(tmp_path, tagged_repo)

    def boom(*args, **kwargs):
        raise RuntimeError("exploded")

    assert watch.check(cfg, runner=boom) == 2
    entry = read_state(tmp_path)[str(tagged_repo)]
    assert entry["last_run_exit"] == 2
    assert "last_tag" not in entry


def test_unreachable_repo_fails_check_but_writes_state(tmp_path, capsys):
    cfg = write_config(tmp_path, tmp_path / "nonexistent-repo")
    assert watch.check(cfg, runner=FakeRunner()) == 2
    state = read_state(tmp_path)
    entry = state[str(tmp_path / "nonexistent-repo")]
    assert entry["last_run_exit"] == 2
    assert "watch" in capsys.readouterr().err


# ---- state file ----


def test_corrupt_state_starts_fresh_with_warning(tagged_repo, tmp_path, capsys):
    cfg = write_config(tmp_path, tagged_repo)
    (tmp_path / "state.json").write_text("{not valid json")
    runner = FakeRunner()
    assert watch.check(cfg, runner=runner) == 0
    assert runner.calls == [("v1.0.0", "v1.1.0")]  # first-run behavior
    assert "state" in capsys.readouterr().err
    assert read_state(tmp_path)[str(tagged_repo)]["last_tag"] == "v1.1.0"


def test_non_object_state_starts_fresh(tagged_repo, tmp_path, capsys):
    cfg = write_config(tmp_path, tagged_repo)
    (tmp_path / "state.json").write_text('["v1.0.0"]')
    runner = FakeRunner()
    assert watch.check(cfg, runner=runner) == 0
    assert runner.calls == [("v1.0.0", "v1.1.0")]
    assert "state" in capsys.readouterr().err


def test_state_file_override(tagged_repo, tmp_path):
    cfg = tmp_path / "targets.toml"
    cfg.write_text(
        'state_file = "nested/state.json"\n'
        f'[[target]]\nrepo = "{tagged_repo}"\n'
    )
    runner = FakeRunner()
    assert watch.check(cfg, runner=runner) == 0
    state = json.loads((tmp_path / "nested" / "state.json").read_text())
    assert state[str(tagged_repo)]["last_tag"] == "v1.1.0"
    assert not (tmp_path / "state.json").exists()


def test_state_write_is_atomic(tagged_repo, tmp_path):
    cfg = write_config(tmp_path, tagged_repo)
    assert watch.check(cfg, runner=FakeRunner()) == 0
    # no temp file is left behind next to the config
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".state.json")]
    assert leftovers == []


# ---- config errors exit 2 ----


def test_missing_config_exits_2(tmp_path, capsys):
    assert cli.main(["watch", "--config", str(tmp_path / "nope.toml")]) == cli.EXIT_ERROR
    assert "error" in capsys.readouterr().err


def test_malformed_config_exits_2(tmp_path):
    cfg = tmp_path / "targets.toml"
    cfg.write_text("[[target]\n")
    assert cli.main(["watch", "--config", str(cfg)]) == cli.EXIT_ERROR


def test_target_without_repo_exits_2(tmp_path):
    cfg = tmp_path / "targets.toml"
    cfg.write_text('[[target]]\nlang = ["go"]\n')
    assert cli.main(["watch", "--config", str(cfg)]) == cli.EXIT_ERROR


def test_fixture_targets_toml_parses():
    fixture = Path(__file__).parent / "fixtures" / "targets.toml"
    targets = config.load_targets(fixture)
    assert len(targets) == 2
    assert targets[0]["lang"] == ["go", "python"]
    assert targets[0]["include"] == ["src/**", "app/**"]


# ---- cli-level end to end (real run pipeline) ----

V1 = '''from flask import Flask

app = Flask(__name__)


@app.get("/health")
def health():
    return "ok"
'''

V2 = '''from flask import Flask

app = Flask(__name__)


@app.get("/health")
def health():
    return "ok"


@app.post("/admin/reset")
def reset():
    return do_reset()
'''


@pytest.fixture
def flask_repo(tmp_path: Path) -> Path:
    repo = init_repo(tmp_path / "target")
    (repo / "app.py").write_text(V1)
    git(repo, "add", "app.py")
    git(repo, "commit", "-qm", "v1")
    tag(repo, "v1.0.0")
    (repo / "app.py").write_text(V2)
    git(repo, "add", "app.py")
    git(repo, "commit", "-qm", "v2")
    tag(repo, "v1.1.0")
    return repo


def _fake_tool(tmp_path: Path, name: str, payload: str) -> Path:
    tool = tmp_path / name
    tool.write_text(f"#!/bin/sh\necho '{payload}'\n")
    tool.chmod(0o755)
    return tool


def test_cli_watch_real_pipeline_advances(flask_repo, tmp_path, monkeypatch):
    """watch wired to the real run pipeline: a delta run exits the check 0
    and advances last_tag; a second watch does no work."""
    egress = _fake_tool(tmp_path, "egress-plane", '{"schema_version": 1, "sites": []}')
    acl = _fake_tool(tmp_path, "acl-guard-audit", '{"schema_version": "acl-guard-audit/v1"}')
    monkeypatch.setenv("RSD_EGRESS_PLANE", str(egress))
    monkeypatch.setenv("RSD_ACL_GUARD_AUDIT", str(acl))
    out = tmp_path / "out"
    cfg = write_config(tmp_path, flask_repo, extra=f'out = "{out}"\n')
    assert cli.main(["watch", "--config", str(cfg)]) == 0
    entry = read_state(tmp_path)[str(flask_repo)]
    assert entry["last_tag"] == "v1.1.0"
    assert entry["last_run_exit"] == 1  # the new route is a delta
    assert entry["degraded"] is False
    pair_dir = out / "target" / "v1.0.0..v1.1.0"
    surface = json.loads((pair_dir / "surface.json").read_text())
    assert "POST /admin/reset" in {r["id"] for r in surface["added"]}
    assert cli.main(["watch", "--config", str(cfg)]) == 0


def test_cli_watch_real_pipeline_degraded(flask_repo, tmp_path, monkeypatch):
    """With no extractor tools on PATH the run degrades: the outcome is
    recorded but last_tag does not advance, so the pair retries."""
    monkeypatch.delenv("RSD_EGRESS_PLANE", raising=False)
    monkeypatch.delenv("RSD_ACL_GUARD_AUDIT", raising=False)
    cfg = write_config(tmp_path, flask_repo, extra=f'out = "{tmp_path / "out"}"\n')
    assert cli.main(["watch", "--config", str(cfg)]) == 0
    entry = read_state(tmp_path)[str(flask_repo)]
    assert entry["last_run_exit"] == 1
    assert entry["degraded"] is True
    assert "last_tag" not in entry
