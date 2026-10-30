"""Git plumbing: repo access, tag resolution, worktrees, diff and log -L.

Local-path repos are used in place; URLs are cloned as `--filter=blob:none`
partial clones so inter-tag history stays available for `git log -L` (KTD4),
falling back to a shallow fetch of just the tag refs when the remote rejects
filters. Worktrees are created under a temp dir outside the target repo and
removed in a finally path plus an atexit backstop (KTD14).
"""

import atexit
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """Any git-layer failure; the CLI maps it to exit code 2."""


def _note(msg: str) -> None:
    print(f"release-surface-diff: {msg}", file=sys.stderr)


def run_git(repo: Path | str | None, *args: str, timeout=None) -> str:
    """Run git (optionally with -C repo) and return stdout, or raise GitError."""
    cmd = ["git"]
    if repo is not None:
        cmd += ["-C", str(repo)]
    cmd += list(args)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise GitError("git is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if res.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def is_url(repo) -> bool:
    s = str(repo)
    return "://" in s or s.startswith("git@")


@dataclass
class Repo:
    """A usable local git repo: either the path the user gave or a clone."""

    path: Path
    cloned: bool = False


def _clone(url: str, dest: Path) -> None:
    """Partial (blobless) clone, falling back to a shallow tag-only fetch."""
    res = subprocess.run(
        ["git", "clone", "--filter=blob:none", "--quiet", url, str(dest)],
        capture_output=True, text=True,
    )
    if res.returncode == 0:
        return
    shutil.rmtree(dest, ignore_errors=True)
    _note("remote rejected --filter=blob:none; falling back to shallow-by-tag fetch")
    run_git(None, "init", "-q", str(dest))
    run_git(dest, "remote", "add", "origin", url)
    run_git(dest, "fetch", "--depth", "1", "origin", "+refs/tags/*:refs/tags/*")


@contextmanager
def open_repo(repo, clone_base=None):
    """Yield a Repo. URL repos are cloned under clone_base (or a temp dir) and
    removed on exit; local paths are validated and used in place."""
    if is_url(repo):
        base = Path(clone_base) if clone_base else None
        if base is not None:
            base.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="rsd-clone-", dir=base))
        dest = root / "repo"
        try:
            _clone(str(repo), dest)
            yield Repo(dest, cloned=True)
        finally:
            shutil.rmtree(root, ignore_errors=True)
    else:
        path = Path(repo)
        if not path.is_dir():
            raise GitError(f"repo path does not exist: {repo}")
        run_git(path, "rev-parse", "--git-dir")
        yield Repo(path, cloned=False)


def repo_slug(repo) -> str:
    """owner/name for remotes (the seed/report contract), else the dir name."""
    s = str(repo).rstrip("/")
    if is_url(s):
        if s.endswith(".git"):
            s = s[:-4]
        s = s.split("://", 1)[1] if "://" in s else s.replace(":", "/")
        parts = [p for p in s.split("/") if p]
        return "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    return Path(s).name


# ---- tags and tolerant semver ordering ----


# v?MAJOR.MINOR(.PATCH)?(-|.pre)?: a missing patch pads to 0, a trailing
# suffix marks a pre-release. Anything else is unparseable and skipped.
_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-.]?(.+))?$")


def _pre_key(suffix: str) -> tuple:
    """Natural-sort a pre-release suffix: rc1 < rc2 < rc10, alpha < beta < rc."""
    key = []
    for piece in re.findall(r"\d+|[^\d]+", suffix):
        key.append((0, int(piece)) if piece.isdigit() else (1, piece))
    return tuple(key)


def parse_tag(tag: str) -> tuple | None:
    """Sort key for a tag, or None if it is not semver-shaped.

    Key shape: (major, minor, patch, is_release, pre_key). A release sorts
    after all its own pre-releases (1.2.0-rc1 < 1.2.0).
    """
    m = _TAG_RE.match(tag.strip())
    if not m:
        return None
    major, minor, patch, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
    if suffix:
        return (int(major), int(minor), int(patch or 0), 0, _pre_key(suffix))
    return (int(major), int(minor), int(patch or 0), 1, ())


def list_tags(repo: Path | str) -> list[str]:
    return [t for t in run_git(repo, "tag", "--list").splitlines() if t.strip()]


def _sorted_parseable_tags(repo) -> list[tuple[tuple, str]]:
    parsed = []
    for tag in list_tags(repo):
        key = parse_tag(tag)
        if key is None:
            _note(f"skipping unparseable tag: {tag}")
        else:
            parsed.append((key, tag))
    parsed.sort(key=lambda item: item[0])
    return parsed


def release_tags(repo, pre: bool = False) -> list[str]:
    """All parseable tags in tolerant semver order, oldest first.

    Pre-releases are excluded unless pre=True, matching --latest; anything
    unparseable is skipped with a note.
    """
    parsed = _sorted_parseable_tags(repo)
    if not pre:
        parsed = [item for item in parsed if item[0][3] == 1]
    return [tag for _key, tag in parsed]


def resolve_latest(repo, pre: bool = False) -> tuple[str, str]:
    """The newest two tags in tolerant semver order: (from, to).

    Pre-releases are ignored unless pre=True. Fewer than two parseable tags
    is a tool error.
    """
    tags = release_tags(repo, pre=pre)
    if len(tags) < 2:
        scope = "parseable tags" if pre else "release tags"
        raise GitError(
            f"fewer than two {scope} in {repo}; pass --from/--to explicitly"
        )
    return tags[-2], tags[-1]


def _rev_exists(repo, ref: str) -> bool:
    res = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True, text=True,
    )
    return res.returncode == 0


def resolve_pair(repo, from_tag: str, to_tag: str) -> tuple[str, str]:
    """Validate an explicit tag pair.

    Both revs must resolve, they must differ, and when both are semver-shaped
    the from side must sort strictly before the to side.
    """
    if from_tag == to_tag:
        raise GitError(f"--from and --to are the same tag: {from_tag}")
    for ref in (from_tag, to_tag):
        if not _rev_exists(repo, ref):
            raise GitError(f"unknown tag {ref!r} in {repo}")
    from_key, to_key = parse_tag(from_tag), parse_tag(to_tag)
    if from_key is not None and to_key is not None and from_key >= to_key:
        raise GitError(
            f"reversed tag pair: {from_tag} sorts after {to_tag} in semver "
            f"order; expected --from {to_tag} --to {from_tag}"
        )
    return from_tag, to_tag


def resolve_commit(repo, ref: str) -> str:
    """Commit sha for a ref; annotated tags peel via ^{commit}."""
    return run_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


# ---- worktrees (KTD14) ----


# (repo, worktree path, temp root) still needing cleanup; the finally path in
# worktrees() clears entries as it cleans, so this only holds crash strays.
_LIVE_WORKTREES: list[tuple[Path, Path, Path]] = []


def _cleanup_strays() -> None:
    for repo, wt, root in list(_LIVE_WORKTREES):
        try:
            run_git(repo, "worktree", "remove", "--force", str(wt))
        except (GitError, OSError):
            pass
        shutil.rmtree(root, ignore_errors=True)
    _LIVE_WORKTREES.clear()


atexit.register(_cleanup_strays)


def _safe_name(ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", ref)


@contextmanager
def worktrees(repo, refs, base_dir=None):
    """Yield {ref: worktree Path} for each ref, checked out under a temp dir
    outside the target repo. `git worktree prune` runs first; every worktree
    is removed in a finally path (and by an atexit backstop on hard exits).
    """
    repo = Path(repo)
    run_git(repo, "worktree", "prune")
    root = Path(tempfile.mkdtemp(prefix="rsd-worktrees-", dir=base_dir))
    added: dict[str, Path] = {}
    try:
        for ref in refs:
            wt = root / _safe_name(ref)
            run_git(repo, "worktree", "add", "--detach", str(wt), ref)
            added[ref] = wt
            _LIVE_WORKTREES.append((repo, wt, root))
        yield added
    finally:
        for wt in added.values():
            try:
                run_git(repo, "worktree", "remove", "--force", str(wt))
            except GitError:
                pass
            try:
                _LIVE_WORKTREES.remove((repo, wt, root))
            except ValueError:
                pass
        shutil.rmtree(root, ignore_errors=True)


# ---- diff and line-range history ----


def git_diff(repo, from_ref: str, to_ref: str, context: int = 5) -> str:
    """`git diff -U5 <from>..<to>` output; feeds the guard hunk classifier."""
    return run_git(repo, "diff", f"-U{context}", f"{from_ref}..{to_ref}")


def log_line_history(
    repo, ref: str, path: str, start: int, end: int,
    *, first_only: bool = True, timeout=None,
) -> str:
    """`git log -L<start>,<end>:<path> <ref>`.

    Callers pass the from-side ref and from-side line range for hunks that
    removed code (KTD10); falls back to the tag commit in seeds.py when this
    finds nothing (e.g. on a shallow clone). `first_only` adds `-1`, so the
    walk stops at the newest touching commit (the attribution answer) instead
    of replaying the range's whole history; `timeout` bounds each call.
    """
    args = ["log"]
    if first_only:
        args.append("-1")
    args += [f"-L{start},{end}:{path}", ref]
    return run_git(repo, *args, timeout=timeout)
