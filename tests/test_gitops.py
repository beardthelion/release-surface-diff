"""Git-layer tests: tag resolution, worktree lifecycle, diff capture, CLI wiring."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from release_surface_diff import cli, gitops


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout


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


@pytest.fixture
def tagged_repo(tmp_path: Path) -> Path:
    """Three semver tags, one of them a pre-release, plus a non-semver tag."""
    repo = init_repo(tmp_path / "target")
    commit_file(repo, "app.txt", "v1 contents\n", "first")
    git(repo, "tag", "v1.0.0")
    git(repo, "tag", "nightly-build")
    commit_file(repo, "app.txt", "v1.1 contents\n", "second")
    git(repo, "tag", "v1.1.0")
    commit_file(repo, "app.txt", "v1.2 rc contents\n", "third")
    git(repo, "tag", "v1.2.0-rc1")
    return repo


def test_latest_ignores_prerelease(tagged_repo: Path, capsys) -> None:
    assert gitops.resolve_latest(tagged_repo) == ("v1.0.0", "v1.1.0")
    note = capsys.readouterr().err
    assert "nightly-build" in note  # unparseable tag skipped with a note


def test_latest_with_pre(tagged_repo: Path) -> None:
    assert gitops.resolve_latest(tagged_repo, pre=True) == ("v1.1.0", "v1.2.0-rc1")


def test_fewer_than_two_tags(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "target")
    commit_file(repo, "a.txt", "x\n", "one")
    git(repo, "tag", "v1.0.0")
    with pytest.raises(gitops.GitError, match="fewer than two"):
        gitops.resolve_latest(repo)


def test_resolve_pair_equal_tags(tagged_repo: Path) -> None:
    with pytest.raises(gitops.GitError, match="same tag"):
        gitops.resolve_pair(tagged_repo, "v1.0.0", "v1.0.0")


def test_resolve_pair_reversed(tagged_repo: Path) -> None:
    with pytest.raises(gitops.GitError) as exc:
        gitops.resolve_pair(tagged_repo, "v1.1.0", "v1.0.0")
    msg = str(exc.value)
    assert "v1.0.0" in msg and "v1.1.0" in msg and "order" in msg


def test_resolve_pair_unknown_tag(tagged_repo: Path) -> None:
    with pytest.raises(gitops.GitError, match="unknown tag"):
        gitops.resolve_pair(tagged_repo, "v9.9.9", "v1.0.0")


def test_open_repo_missing_path(tmp_path: Path) -> None:
    with pytest.raises(gitops.GitError):
        with gitops.open_repo(tmp_path / "nope"):
            pass


def test_open_repo_not_a_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(gitops.GitError):
        with gitops.open_repo(plain):
            pass


def test_worktrees_outside_repo_and_cleaned(tagged_repo: Path) -> None:
    with gitops.worktrees(tagged_repo, ["v1.0.0", "v1.1.0"]) as wts:
        older, newer = wts["v1.0.0"], wts["v1.1.0"]
        assert older.is_dir() and newer.is_dir()
        # never inside the target repo
        assert not str(older).startswith(str(tagged_repo) + "/")
        assert not str(newer).startswith(str(tagged_repo) + "/")
        assert (older / "app.txt").read_text() == "v1 contents\n"
        assert (newer / "app.txt").read_text() == "v1.1 contents\n"
    assert not older.exists() and not newer.exists()
    listed = git(tagged_repo, "worktree", "list", "--porcelain")
    assert str(older) not in listed and str(newer) not in listed


def test_worktrees_cleaned_on_exception(tagged_repo: Path) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with gitops.worktrees(tagged_repo, ["v1.0.0"]) as wts:
            wt = wts["v1.0.0"]
            assert wt.is_dir()
            raise RuntimeError("boom")
    assert not wt.exists()
    listed = git(tagged_repo, "worktree", "list", "--porcelain")
    assert str(wt) not in listed


def test_diff_capture(tagged_repo: Path) -> None:
    diff = gitops.git_diff(tagged_repo, "v1.0.0", "v1.1.0")
    assert "-v1 contents" in diff
    assert "+v1.1 contents" in diff
    assert "@@" in diff


def test_log_line_history(tagged_repo: Path) -> None:
    out = gitops.log_line_history(tagged_repo, "v1.1.0", "app.txt", 1, 1)
    assert "v1.1 contents" in out or "commit" in out.lower()


def test_repo_slug(tagged_repo: Path) -> None:
    assert gitops.repo_slug("https://github.com/owner/name.git") == "owner/name"
    assert gitops.repo_slug("git@github.com:owner/name.git") == "owner/name"
    assert gitops.repo_slug(str(tagged_repo)) == "target"


def test_open_repo_file_url_clone(tagged_repo: Path, tmp_path: Path) -> None:
    """URL handling without network: file:// exercises the clone path."""
    url = f"file://{tagged_repo}"
    with gitops.open_repo(url, tmp_path / "clones") as repo:
        assert repo.path.is_dir()
        assert "v1.1.0" in gitops.list_tags(repo.path)
        assert gitops.resolve_latest(repo.path) == ("v1.0.0", "v1.1.0")
    assert not repo.path.exists()


# ---- CLI wiring ----


def test_cli_run_latest(tagged_repo: Path, capsys) -> None:
    code = cli.main(["run", "--repo", str(tagged_repo), "--latest"])
    assert code == cli.EXIT_DELTA  # v1.0.0..v1.1.0 differs
    listed = git(tagged_repo, "worktree", "list", "--porcelain")
    assert "rsd-worktrees" not in listed


def test_cli_run_pair_no_diff(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "target")
    commit_file(repo, "a.txt", "same\n", "one")
    git(repo, "tag", "v1.0.0")
    git(repo, "tag", "v1.1.0")  # same commit, second tag
    code = cli.main(
        ["run", "--repo", str(repo), "--from", "v1.0.0", "--to", "v1.1.0"]
    )
    assert code == cli.EXIT_NO_DELTA


def test_cli_run_nonexistent_repo(tmp_path: Path, capsys) -> None:
    code = cli.main(
        ["run", "--repo", str(tmp_path / "nope"), "--from", "a", "--to", "b"]
    )
    assert code == cli.EXIT_ERROR
    assert "error" in capsys.readouterr().err


def test_cli_run_from_equals_to(tagged_repo: Path, capsys) -> None:
    code = cli.main(
        ["run", "--repo", str(tagged_repo), "--from", "v1.0.0", "--to", "v1.0.0"]
    )
    assert code == cli.EXIT_ERROR


def test_cli_run_reversed(tagged_repo: Path, capsys) -> None:
    code = cli.main(
        ["run", "--repo", str(tagged_repo), "--from", "v1.1.0", "--to", "v1.0.0"]
    )
    assert code == cli.EXIT_ERROR
    assert "order" in capsys.readouterr().err


def test_cli_run_requires_pair_or_latest(tagged_repo: Path, capsys) -> None:
    assert cli.main(["run", "--repo", str(tagged_repo)]) == cli.EXIT_ERROR
    assert (
        cli.main(["run", "--repo", str(tagged_repo), "--from", "v1.0.0"])
        == cli.EXIT_ERROR
    )


def test_cli_extract_smoke(capsys) -> None:
    fixture = Path(__file__).parent / "fixtures" / "go" / "basic"
    code = cli.main(["extract", "--root", str(fixture), "--lang", "go"])
    assert code == 0
    for line in capsys.readouterr().out.splitlines():
        json.loads(line)  # every stdout line must be a JSON record


def test_cli_extract_missing_root(tmp_path: Path, capsys) -> None:
    code = cli.main(["extract", "--root", str(tmp_path / "nope"), "--lang", "go"])
    assert code == cli.EXIT_ERROR


def test_cli_watch_stub(tmp_path: Path, capsys) -> None:
    cfg = tmp_path / "targets.toml"
    cfg.write_text('[[target]]\nrepo = "https://example.com/x/y"\n')
    assert cli.main(["watch", "--config", str(cfg)]) == cli.EXIT_ERROR
    assert "watch" in capsys.readouterr().err


def test_help_lists_subcommands(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for sub in ("run", "extract", "watch"):
        assert sub in out
