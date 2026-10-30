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
MAX_LOG_L_CALLS = 2000

_COMMIT_RE = re.compile(r"^commit ([0-9a-f]{40})\b", re.MULTILINE)


def _hunk_lookup(hunk: GuardHunk, base_ref: str, to_ref: str):
    """(rev-range, path, start, end) for the `log -L` attribution of one
    hunk. The range runs from the merge base of the tag pair so commits on
    the to side are covered even when the tags diverged (release
    branches).

    Removed hunks resolve on the from side: their lines do not exist at the
    to tag, so history must be traced where they still lived (KTD10). Added
    and modified hunks resolve on the to side, where the lines were written.
    """
    # Added/modified lines resolve on the to side: `log -L` interprets the
    # range at the tip, which is where those lines live. Removed lines are
    # gone at the tip, so to-side numbers are meaningless there; they go
    # through `_removing_commit` instead (spec R15).
    rev = f"{base_ref}..{to_ref}"
    if hunk.direction == "removed":
        end = hunk.old_start + hunk.old_count - 1
        return rev, hunk.old_file, hunk.old_start, end
    end = hunk.new_start + hunk.new_count - 1
    return rev, hunk.file, hunk.new_start, end


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
        out = ""
    m = _COMMIT_RE.search(out)
    if m:
        return m.group(1)
    # -L cannot resolve lines in a file created or deleted inside the
    # range; the newest path-touching commit is the real answer there.
    return gitops.path_touching_commit(repo, ref, path, timeout=timeout)


_BLAME_SHA_RE = re.compile(r"^\^?([0-9a-f]{40})\b", re.MULTILINE)


def _removing_commit(repo, base_ref: str, to_ref: str, path: str,
                     start: int, end: int, timeout) -> "str | None":
    """The first-parent child, within base..to, of the last commit where the
    removed lines still existed (reverse blame), or None when unresolvable."""
    if start <= 0 or end < start:
        return None
    try:
        blame = gitops.blame_reverse(
            repo, base_ref, to_ref, path, start, end, timeout=timeout
        )
        chain = gitops.first_parent_chain(repo, base_ref, to_ref, timeout=timeout)
        base_sha = gitops.resolve_commit(repo, base_ref)
    except GitError:
        blame = None
    if blame is not None:
        # child_on_path[sha] = sha's successor on the first-parent path,
        # where the path begins at the base commit itself.
        order = [base_sha] + chain
        child_on_path = {s: order[i + 1] for i, s in enumerate(order)
                         if i + 1 < len(order)}
        for last_seen in _BLAME_SHA_RE.findall(blame):
            child = child_on_path.get(last_seen)
            if child:
                return child
    # Deleted-file hunks (or blame failures) resolve through the newest
    # path-touching commit in the range, which is the deleter.
    return gitops.path_touching_commit(
        repo, f"{base_ref}..{to_ref}", path, timeout=timeout
    )


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
    base_ref: str,
    to_ref: str,
    to_commit: "str | None" = None,
    allow_lookup: bool = True,
    timeout=LOG_L_TIMEOUT,
    parent_cache: "dict | None" = None,
) -> dict:
    """One seeds.jsonl record for a classified hunk.

    `base_ref` is the merge base of the tag pair (from_ref when the tags
    are linear); every history lookup runs over base..to.
    """
    repo_path = Path(repo_path)
    if to_commit is None:
        to_commit = gitops.resolve_commit(repo_path, to_ref)
    sha = None
    if allow_lookup:
        ref, path, start, end = _hunk_lookup(hunk, base_ref, to_ref)
        if hunk.direction == "removed":
            sha = _removing_commit(
                repo_path, base_ref, to_ref, path, start, end, timeout
            )
        else:
            sha = _introducing_commit(
                repo_path, ref, path, start, end, timeout
            )
    attributed = sha is not None
    if sha is None:
        sha = to_commit
    files = [hunk.file]
    if hunk.direction == "removed" and hunk.old_file not in files:
        files.append(hunk.old_file)
    seed = {
        "repo": repo_name,
        "sha": sha,
        "parent": _first_parent(repo_path, sha, parent_cache),
        "source": SOURCE,
        "class": hunk.hunk_class,
        "files": files,
        "symbol": hunk.symbol,
        "evidence": hunk.evidence,
    }
    if not attributed:
        # lookup failed or found nothing: the tag-commit fallback is explicit
        seed["attribution"] = "fallback"
    return seed


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
    if not gitops.is_url(str(repo_arg)):
        # repo_path may be a clone whose origin is the temp clone URL; ask the
        # user's repo itself for the real remote (spec: owner/name).
        origin = gitops.origin_url(repo_arg)
        if origin:
            repo_name = gitops.repo_slug(origin)
    to_commit = gitops.resolve_commit(repo_path, to_ref)
    # Release branches diverge: from_ref is not an ancestor of to_ref, so
    # from..to would exclude the commits the diff is actually made of.
    # The merge base is the range start for every lookup; when the refs
    # share no ancestor, from_ref is the least-wrong fallback.
    base = gitops.merge_base(repo_path, from_ref, to_ref) or from_ref
    parent_cache: dict = {}
    seeds = []
    for i, hunk in enumerate(hunks):
        seeds.append(
            seed_for_hunk(
                hunk,
                repo_path=repo_path,
                repo_name=repo_name,
                base_ref=base,
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
