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
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, gitops

STATE_FILE_NAME = "state.json"


def _note(msg: str) -> None:
    print(f"release-surface-diff: {msg}", file=sys.stderr)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    return data


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


def _pending_pairs(repo_path, last_tag) -> list[tuple[str, str]]:
    """(from, to) tag pairs to run for one target, oldest new tag first.

    Release tags only, pre-releases excluded exactly as in --latest. With
    no usable last_tag only the newest pair is pending: the first check
    diffs the latest release, it does not replay history. Otherwise every
    tag newer than last_tag pairs with the tag that precedes it in semver
    order.
    """
    tags = gitops.release_tags(repo_path)
    if len(tags) < 2:
        raise gitops.GitError(f"fewer than two release tags in {repo_path}")
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
    with gitops.open_repo(repo_arg) as repo:
        for from_tag, to_tag in _pending_pairs(repo.path, entry.get("last_tag")):
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
    default_out = options.get("out") or "out"
    failed = False
    for target in targets:
        key = str(target["repo"])
        entry = dict(state.get(key) or {})
        entry["last_checked"] = _now()
        try:
            if _check_target(target, entry, runner, default_out):
                failed = True
        except (gitops.GitError, config.ConfigError) as exc:
            _note(f"watch: {key}: {exc}")
            entry["last_run_exit"] = 2
            failed = True
        entry.setdefault("last_run_exit", 0)
        entry.setdefault("degraded", False)
        state[key] = entry
    try:
        _write_state(state_path, state)
    except OSError as exc:
        _note(f"watch: could not write state file {state_path}: {exc}")
        return 2
    return 2 if failed else 0
