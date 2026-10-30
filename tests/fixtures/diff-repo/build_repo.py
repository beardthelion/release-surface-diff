"""Build the synthetic diff-repo fixture (spec test plan item 2, U10).

Creates a three-commit git repo at ``dest`` carrying the spec's synthetic
change set, python-only so the in-repo py_ts_routes extractor covers it
with no external tools:

- v1.0.0 -> v1.1.0: one added route (``POST /admin/rotate-keys``), one
  removed CLI flag (``--skip-verify``; python has no cli_flag extractor,
  so the removal is observable as a removed auth-class guard hunk), one
  renamed handler (``GET /users/{id}`` -> ``GET /members/{id}`` with an
  identical body), and one removed bounds check in ``decode_payload``.
- v1.1.1 tags the same commit as v1.1.0: the pair v1.1.0..v1.1.1 diffs to
  nothing and must exit 0.
- v1.2.0 adds a bounds check with no surface change: a guard-only delta
  must still exit 1 (R4).

Commits use a fixed author, committer, and date, so SHAs are reproducible
across machines and seeds.jsonl attribution is stable. Usage:

    python build_repo.py <dest>
"""

import os
import subprocess
import sys
from pathlib import Path

TAG_V1 = "v1.0.0"
TAG_V2 = "v1.1.0"
TAG_V2_ALIAS = "v1.1.1"
TAG_V3 = "v1.2.0"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_AUTHOR_DATE": "2026-10-29T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-10-29T00:00:00Z",
}

MANIFEST = """\
[project]
name = "synthetic-diff-target"
version = "1.0.0"
"""

APP_V1 = '''\
"""Synthetic release-diff fixture: a tiny Flask-style app (v1)."""

from flask import Flask, abort, request

app = Flask(__name__)

MAX_BODY = 4096


@app.get("/health")
def health():
    return "ok"


@app.get("/users/<id>")
def user_detail(id):
    user = load_user(id)
    if user is None:
        abort(404)
    payload = {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "created": user.created,
        "roles": sorted(user.roles),
    }
    return payload


def decode_payload(data):
    if len(data) > MAX_BODY:
        abort(413)
    return parse(data)
'''

APP_V2 = '''\
"""Synthetic release-diff fixture: a tiny Flask-style app (v2)."""

from flask import Flask, abort, request

app = Flask(__name__)

MAX_BODY = 4096


@app.post("/admin/rotate-keys")
def rotate_keys():
    return {"rotated": rotate_all_keys()}


@app.get("/health")
def health():
    return "ok"


@app.get("/members/<id>")
def user_detail(id):
    user = load_user(id)
    if user is None:
        abort(404)
    payload = {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "created": user.created,
        "roles": sorted(user.roles),
    }
    return payload


def decode_payload(data):
    return parse(data)
'''

APP_V3 = '''\
"""Synthetic release-diff fixture: a tiny Flask-style app (v3)."""

from flask import Flask, abort, request

app = Flask(__name__)

MAX_BODY = 4096


@app.post("/admin/rotate-keys")
def rotate_keys():
    return {"rotated": rotate_all_keys()}


@app.get("/health")
def health():
    return "ok"


@app.get("/members/<id>")
def user_detail(id):
    user = load_user(id)
    if user is None:
        abort(404)
    payload = {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "created": user.created,
        "roles": sorted(user.roles),
    }
    return payload


def decode_payload(data):
    limit = MAX_BODY * 2
    if len(data) > limit:
        raise ValueError("payload too large")
    return parse(data)
'''

CLI_V1 = '''\
"""CLI flags for the synthetic fixture app (v1)."""

import argparse
import ssl


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="synthetic")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--skip-verify", action="store_true")
    return parser.parse_args(argv)


def tls_context(args):
    ctx = ssl.create_default_context()
    if args.skip_verify:
        ctx.verify_mode = ssl.CERT_NONE
        ctx.check_hostname = False
    return ctx
'''

CLI_V2 = '''\
"""CLI flags for the synthetic fixture app (v2)."""

import argparse
import ssl


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="synthetic")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default="8080")
    return parser.parse_args(argv)


def tls_context(args):
    return ssl.create_default_context()
'''

FILES_V1 = {"pyproject.toml": MANIFEST, "app.py": APP_V1, "cli.py": CLI_V1}
FILES_V2 = {"pyproject.toml": MANIFEST, "app.py": APP_V2, "cli.py": CLI_V2}
FILES_V3 = {"pyproject.toml": MANIFEST, "app.py": APP_V3, "cli.py": CLI_V2}


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(_GIT_ENV)
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return res.stdout


def _write_all(repo: Path, files: dict) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _commit_all(repo: Path, files: dict, msg: str) -> None:
    _write_all(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)


def build(dest) -> Path:
    """Materialize the tagged fixture repo at ``dest`` and return its path."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    _git(dest, "init", "-q", ".")
    _git(dest, "config", "commit.gpgsign", "false")
    _git(dest, "config", "tag.gpgsign", "false")
    _commit_all(dest, FILES_V1, "v1")
    _git(dest, "tag", TAG_V1)
    _commit_all(dest, FILES_V2, "v2")
    _git(dest, "tag", TAG_V2)
    _git(dest, "tag", TAG_V2_ALIAS)
    _commit_all(dest, FILES_V3, "v3")
    _git(dest, "tag", TAG_V3)
    return dest


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: build_repo.py <dest>", file=sys.stderr)
        return 2
    build(argv[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
