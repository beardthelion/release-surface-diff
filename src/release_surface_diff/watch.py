"""Watch mode: a one-shot new-tag check per target with atomic state (KTD9).

`watch --config targets.toml` is finite per invocation; scheduling is the
operator's job. For each `[[target]]` the release tags are compared to
state.json's last_tag in the same tolerant semver order `--latest` uses,
and the previous-tag pair runs once per new tag, oldest first. A run that
fails (exit 2) or that extracted degraded (a missing or partial extractor)
records the outcome but does not advance last_tag, so the pair retries on
the next invocation.

state.json lives next to targets.toml unless the file sets `state_file`;
it maps each target's repo string to
`{last_tag, last_run_exit, last_checked, degraded}` and is written via
tmp-file-plus-rename. watch exits 0 when the check completes regardless of
deltas found, 2 when a target run fails or the config is invalid.
"""

import json
import os
import tempfile
from pathlib import Path

from . import config, gitops
from ._util import note as _note, utc_now

STATE_FILE_NAME = "state.json"


def _state_path(config_path, options) -> Path:
    """state.json next to targets.toml; a `state_file` top-level key
    overrides it (a relative path resolves against the config directory).
    """
    base = Path(config_path).resolve().parent
    override = options.get("state_file")
    if override:
        p = Path(override)
        return p if p.is_absolute() else base / p
    return base / STATE_FILE_NAME


def _load_state(path) -> dict:
    """Per-target state keyed by repo string.

    A missing file is the normal first-run case; an unreadable or
    non-object file starts fresh with a warning (KTD9).
    """
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        _note(f"corrupt state file {path}; starting fresh ({exc})")
        return {}
    if not isinstance(data, dict):
        _note(f"state file {path} is not a JSON object; starting fresh")
        return {}
    clean = {}
    for key, val in data.items():
        if isinstance(val, dict):
            clean[key] = val
        else:
            _note(f"state file {path}: dropping non-object entry for {key!r}")
    return clean


def _write_state(path, state) -> None:
    """Atomic write: a temp file in the same directory, then rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _tag_names(repo_arg: str) -> list[str]:
    """Tag names without cloning: `ls-remote --tags` for URLs, `tag
    --list` for local paths."""
    if not gitops.is_url(repo_arg):
        return gitops.list_tags(repo_arg)
    gitops._check_url(repo_arg)
    names = []
    for line in gitops.run_git(
        None, "ls-remote", "--tags", repo_arg
    ).splitlines():
        ref = line.partition("\t")[2].strip()
        if ref.startswith("refs/tags/") and not ref.endswith("^{}"):
            names.append(ref[len("refs/tags/"):])
    return names


def _release_tags(names) -> list[str]:
    """gitops.release_tags ordering over an already-collected name list."""
    parsed = []
    for tag in names:
        key = gitops.parse_tag(tag)
        if key is None:
            _note(f"skipping unparseable tag: {tag}")
        else:
            parsed.append((key, tag))
    parsed.sort(key=lambda item: item[0])
    return [tag for key, tag in parsed if key[3] == 1]


def _pending_pairs(tags, last_tag) -> list[tuple[str, str]]:
    """(from, to) tag pairs to run for one target, oldest new tag first.

    `tags` is the release-sorted list from _release_tags (pre-releases
    excluded exactly as in --latest). With no usable last_tag only the
    newest pair is pending: the first check diffs the latest release, it
    does not replay history. Otherwise every tag newer than last_tag
    pairs with the tag that precedes it in semver order.
    """
    if len(tags) < 2:
        raise gitops.GitError("fewer than two release tags")
    last_key = gitops.parse_tag(str(last_tag)) if last_tag else None
    if last_tag and last_key is None:
        _note(
            f"recorded last_tag {last_tag!r} is not semver-shaped; "
            "treating this as a first run"
        )
    if last_key is None:
        return [(tags[-2], tags[-1])]
    pairs = []
    for i, tag in enumerate(tags):
        if gitops.parse_tag(tag) <= last_key:
            continue
        # A new tag with no predecessor in the repo (every tag is newer
        # than the recorded state) pairs against the recorded tag itself.
        prev = tags[i - 1] if i > 0 else str(last_tag)
        pairs.append((prev, tag))
    return pairs


def _default_runner():
    """Late-bound so watch.py carries no import-time cli dependency."""
    from . import cli
    return cli.run_pair


def _check_target(target, entry, runner, default_out) -> bool:
    """Run one target's pending pairs, updating its state entry in place.

    Returns True when a run failed (exit >= 2 or raised). On a failed or
    degraded run the outcome is recorded but last_tag does not advance and
    later pending tags are left for the next invocation (KTD9).
    """
    repo_arg = str(target["repo"])
    failed = False
    langs = config.parse_langs(target.get("lang"))
    include = tuple(target.get("include") or ())
    exclude = target.get("exclude")
    out = target.get("out") or default_out
    names = _tag_names(repo_arg)
    last_tag = entry.get("last_tag")
    if last_tag and str(last_tag) not in names:
        _note(
            f"watch: {repo_arg}: recorded last_tag {last_tag!r} is not a "
            "tag in the repo; treating this as a first run"
        )
        entry.pop("last_tag", None)
        last_tag = None
    pending = _pending_pairs(_release_tags(names), last_tag)
    pending = pending[: target.get("max_pairs", 10)]
    if not pending:
        _note(f"watch: {repo_arg}: no new release tags; skipping clone")
        return failed
    entry["last_checked"] = utc_now()
    with gitops.open_repo(repo_arg) as repo:
        for from_tag, to_tag in pending:
            try:
                code, degraded = runner(
                    repo_arg, repo.path, from_tag, to_tag,
                    langs=langs, include=include, exclude=exclude, out=out,
                )
            except Exception as exc:
                _note(f"watch: {repo_arg} {from_tag}..{to_tag} failed: {exc}")
                code, degraded = 2, False
            entry["last_run_exit"] = code
            entry["degraded"] = bool(degraded)
            if code >= 2 or degraded:
                failed = failed or code >= 2
                break
            entry["last_tag"] = to_tag
    return failed


def check(config_path, *, runner=None) -> int:
    """One-shot watch check over every [[target]] in the config.

    Returns 0 when the check completed, 2 when a target run failed or the
    config was unusable. `runner(repo_arg, repo_path, from_tag, to_tag, *,
    langs, include, exclude, out) -> (exit_code, degraded)` is injectable
    for tests; it defaults to cli.run_pair, the pipeline `run` uses.
    """
    runner = runner or _default_runner()
    config_path = Path(config_path)
    targets, options = config.load_watch_config(config_path)
    state_path = _state_path(config_path, options)
    state = _load_state(state_path)
    loaded = {k: dict(v) for k, v in state.items()}
    default_out = options.get("out") or "out"
    failed = False
    for target in targets:
        key = str(target["repo"])
        entry = dict(state.get(key) or {})
        try:
            if _check_target(target, entry, runner, default_out):
                failed = True
        except Exception as exc:
            # One target's unexpected crash must not abort later targets
            # or skip the state write.
            _note(f"watch: {key}: {exc}")
            entry["last_run_exit"] = 2
            failed = True
        if entry != (state.get(key) or {}):
            entry.setdefault("last_run_exit", 0)
            entry.setdefault("degraded", False)
            state[key] = entry
    if state != loaded:
        try:
            _write_state(state_path, state)
        except OSError as exc:
            _note(f"watch: could not write state file {state_path}: {exc}")
            return 2
    return 2 if failed else 0
