"""In-repo orchestrator-side extractors (KTD1).

`py_ts_routes` covers the Python/TS HTTP-route surface acl-guard-audit
cannot see (KD1); `protocol_ids` is the single producer of bare-literal
req/resp and gossip-topic records across every language (R10).
"""

import os
from pathlib import Path

_PRUNE_DIRS = frozenset({".git", "node_modules", "target", "vendor"})


def iter_files(root, exts, matcher, warn=None):
    """Yield (rel_path, text) for each in-scope file under ``root``.

    os.walk-based so pruned dirs and symlinked entries are never
    enumerated; ``matcher`` gates the repo-relative path. Unreadable
    files are reported through ``warn(rel, exc)`` when given.
    """
    root = Path(root)
    exts = {str(e).lower() for e in exts}
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _PRUNE_DIRS
            and not (Path(dirpath) / d).is_symlink()
        ]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in exts:
                paths.append(p)
    for p in sorted(paths):
        if p.is_symlink() or not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if not matcher.matches(rel):
            continue
        try:
            yield rel, p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            if warn is not None:
                warn(rel, exc)
