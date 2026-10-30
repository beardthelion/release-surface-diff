"""Shared small utilities: stderr notes, UTC timestamps, canonical JSON
serialization, and file output. One implementation for the modules that
emit the four output artifacts (R14, R16)."""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def note(msg: str) -> None:
    print(f"release-surface-diff: {msg}", file=sys.stderr)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(data) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def write_text(path, text: str) -> Path:
    """Atomic write: a temp file in the same directory, then rename."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p
