"""seeds.jsonl: one line per guard hunk, in the cve-variant-sweep seed shape
(R15):

    {"repo","sha","parent","source":"release-diff","class","files","symbol",
     "evidence"}

`sha` is the commit that introduced the hunk, resolved by `git log -L` on
the hunk's line range (from-side range for removed hunks, KTD10); when that
fails -- shallow clone, binary file, range miss, timeout -- it falls back
to the to-tag commit (annotated tags peel via `^{commit}`). `parent` is the
resolved commit's first parent, null when it is a root. `repo` is the
seed-contract slug: owner/name for remotes, the directory name for locals.

`log -L` is bounded twice so a big diff cannot stall a run: a per-call
timeout plus a cap on total lookups per run. Lookups stop at the first
touching commit (`log_line_history` passes `-1`), which is the answer
attribution needs.
"""

import json
import re
from pathlib import Path

from . import gitops
from ._util import write_text
from .gitops import GitError
from .guards import GuardHunk

SOURCE = "release-diff"

# Per-call bound and per-run cap on `git log -L` lookups; hunks past the cap
# fall back to the tag commit rather than stalling the run.
LOG_L_TIMEOUT = 15.0
MAX_LOG_L_CALLS = 200

_COMMIT_RE = re.compile(r"^commit ([0-9a-f]{40})\b", re.MULTILINE)


def _hunk_lookup(hunk: GuardHunk, from_ref: str, to_ref: str):
    """(ref, path, start, end) for the `log -L` attribution of one hunk.

    Removed hunks resolve on the from side: their lines do not exist at the
    to tag, so history must be traced where they still lived (KTD10). Added
    and modified hunks resolve on the to side, where the lines were written.
    """
    if hunk.direction == "removed":
        end = hunk.old_start + hunk.old_count - 1
        return from_ref, hunk.old_file, hunk.old_start, end
    end = hunk.new_start + hunk.new_count - 1
    return to_ref, hunk.file, hunk.new_start, end


def _introducing_commit(repo, ref: str, path: str, start: int, end: int,
                        timeout) -> "str | None":
    """Newest commit touching the range at `ref`, or None when unresolvable."""
    if start <= 0 or end < start:
        return None
    try:
        out = gitops.log_line_history(
            repo, ref, path, start, end, timeout=timeout
        )
    except GitError:
        return None
    m = _COMMIT_RE.search(out)
    return m.group(1) if m else None


def _first_parent(repo, sha: str, cache: "dict | None" = None) -> "str | None":
    """First parent of `sha`, or None for a root commit; memoized per run."""
    if cache is not None and sha in cache:
        return cache[sha]
    try:
        parent = gitops.run_git(
            repo, "rev-parse", "--verify", "--quiet", f"{sha}^"
        ).strip() or None
    except GitError:
        parent = None
    if cache is not None:
        cache[sha] = parent
    return parent


def seed_for_hunk(
    hunk: GuardHunk,
    *,
    repo_path,
    repo_name: str,
    from_ref: str,
    to_ref: str,
    to_commit: "str | None" = None,
    allow_lookup: bool = True,
    timeout=LOG_L_TIMEOUT,
    parent_cache: "dict | None" = None,
) -> dict:
    """One seeds.jsonl record for a classified hunk."""
    repo_path = Path(repo_path)
    if to_commit is None:
        to_commit = gitops.resolve_commit(repo_path, to_ref)
    sha = None
    if allow_lookup:
        ref, path, start, end = _hunk_lookup(hunk, from_ref, to_ref)
        sha = _introducing_commit(repo_path, ref, path, start, end, timeout)
    if sha is None:
        sha = to_commit
    return {
        "repo": repo_name,
        "sha": sha,
        "parent": _first_parent(repo_path, sha, parent_cache),
        "source": SOURCE,
        "class": hunk.hunk_class,
        "files": [hunk.file],
        "symbol": hunk.symbol,
        "evidence": hunk.evidence,
    }


def build_seeds(
    repo_path,
    repo_arg,
    hunks: list[GuardHunk],
    from_ref: str,
    to_ref: str,
    *,
    max_lookups: int = MAX_LOG_L_CALLS,
    timeout=LOG_L_TIMEOUT,
) -> list[dict]:
    """seeds.jsonl records for every hunk, in hunk order (R16).

    `repo_path` is the local repo git queries run against; `repo_arg` is the
    user-supplied --repo value whose slug names the repo field.
    """
    repo_path = Path(repo_path)
    repo_name = gitops.repo_slug(repo_arg)
    to_commit = gitops.resolve_commit(repo_path, to_ref)
    parent_cache: dict = {}
    seeds = []
    for i, hunk in enumerate(hunks):
        seeds.append(
            seed_for_hunk(
                hunk,
                repo_path=repo_path,
                repo_name=repo_name,
                from_ref=from_ref,
                to_ref=to_ref,
                to_commit=to_commit,
                allow_lookup=i < max_lookups,
                timeout=timeout,
                parent_cache=parent_cache,
            )
        )
    return seeds


def seeds_jsonl_text(seeds: list[dict]) -> str:
    """One sorted-key JSON object per line; stable across runs (R16)."""
    return "".join(json.dumps(s, sort_keys=True) + "\n" for s in seeds)


def write_seeds_jsonl(seeds: list[dict], path: "str | Path") -> Path:
    """Write seeds.jsonl, creating the output directory if needed."""
    return write_text(path, seeds_jsonl_text(seeds))
