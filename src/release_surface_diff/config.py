"""Flag helpers, the include/exclude matcher, and target-file loading.

The PathMatcher is the single gate used at both filter points (KTD11): it
drops records whose `file` is excluded, and it decides which paths the guard
hunk classifier scans in the diff. Defaults exclude tests, vendored trees,
generated code, and docs.
"""

import fnmatch
import tomllib
from pathlib import Path, PurePosixPath

from .records import LANGS, normalize_lang


class ConfigError(RuntimeError):
    """Bad flag values or an unusable targets.toml; maps to exit code 2."""


# Matched against repo-relative posix paths. A pattern without a slash applies
# to any path part; a pattern with slashes applies to the whole path, and the
# `**/` variants cover non-root locations.
DEFAULT_EXCLUDE = (
    # tests
    "tests/**", "test/**", "testdata/**",
    "**/tests/**", "**/test/**", "**/testdata/**",
    "*_test.go", "*_test.py", "test_*.py", "*_test.rs",
    "*.test.*", "*.spec.*",
    # vendored / dependency trees
    "vendor/**", "**/vendor/**",
    "third_party/**", "**/third_party/**",
    "node_modules/**", "**/node_modules/**",
    # generated code
    "generated/**", "**/generated/**",
    "*.gen.*", "*.generated.*",
    "*.pb.go", "*.pb.rs", "*_pb2.py", "*.connect.go",
    # docs
    "docs/**", "doc/**", "**/docs/**", "**/doc/**",
    "*.md", "*.rst", "*.adoc",
)


def _match_one(path: str, pattern: str) -> bool:
    """gitignore-flavored match of one pattern against a posix repo path."""
    if "/" not in pattern:
        return any(fnmatch.fnmatch(part, pattern) for part in path.split("/"))
    if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, f"*/{pattern}"):
        return True
    return PurePosixPath(path).match(pattern)


class PathMatcher:
    """Decides whether a repo-relative path is in scope.

    A path is kept when it matches at least one include glob (or no includes
    were given) and no exclude glob. Excludes default to DEFAULT_EXCLUDE.
    """

    def __init__(self, include=(), exclude=None):
        self.include = tuple(include or ())
        self.exclude = tuple(DEFAULT_EXCLUDE if exclude is None else exclude)

    def matches(self, path) -> bool:
        p = str(path)
        if p.startswith("./"):
            p = p[2:]
        p = p.lstrip("/")
        if self.include and not any(_match_one(p, g) for g in self.include):
            return False
        return not any(_match_one(p, g) for g in self.exclude)


def parse_langs(value) -> tuple[str, ...]:
    """`--lang go,rust` or a repeated flag into canonical lang tokens."""
    if not value:
        return ()
    if isinstance(value, str):
        value = [value]
    langs = []
    for item in value:
        for token in str(item).split(","):
            token = token.strip()
            if token:
                try:
                    langs.append(normalize_lang(token))
                except ValueError as exc:
                    raise ConfigError(str(exc)) from exc
    return tuple(dict.fromkeys(langs))


_MANIFESTS = (
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("requirements.txt", "python"),
    ("package.json", "ts"),
    ("tsconfig.json", "ts"),
)


def detect_languages(root: Path) -> tuple[str, ...]:
    """Autodetect `--lang` from manifest files at a tree root (R1)."""
    found = {lang for name, lang in _MANIFESTS if (Path(root) / name).exists()}
    return tuple(lang for lang in LANGS if lang in found)


def load_watch_config(path) -> tuple[list[dict], dict]:
    """(targets, options) from targets.toml for `watch` (U9).

    `targets` is the validated [[target]] list; `options` carries the other
    top-level keys (`state_file`, `out`) that steer the check itself.
    """
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"targets file not found: {path}")
    try:
        data = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid targets file {path}: {exc}") from exc
    targets = data.get("target", [])
    if not isinstance(targets, list):
        raise ConfigError(
            f"targets file {path}: 'target' must be a list of tables"
        )
    seen_repos = set()
    for i, target in enumerate(targets):
        if not isinstance(target, dict):
            raise ConfigError(
                f"targets file {path}: target #{i + 1} is not a table"
            )
        if "repo" not in target:
            raise ConfigError(f"targets file {path}: target #{i + 1} has no repo")
        repo = str(target["repo"])
        if repo in seen_repos:
            raise ConfigError(
                f"targets file {path}: duplicate repo {repo!r}"
            )
        seen_repos.add(repo)
        if "lang" in target:
            parse_langs(target["lang"])  # validate tokens now, not at run time
        for key in ("include", "exclude"):
            globs = target.get(key)
            if globs is not None and (
                not isinstance(globs, list)
                or not all(isinstance(g, str) for g in globs)
            ):
                raise ConfigError(
                    f"targets file {path}: target #{i + 1} '{key}' "
                    "must be a list of strings"
                )
    options = {k: v for k, v in data.items() if k != "target"}
    return targets, options


def load_targets(path) -> list[dict]:
    """Parse `targets.toml` for `watch` (U9 owns the run semantics)."""
    targets, _options = load_watch_config(path)
    return targets
