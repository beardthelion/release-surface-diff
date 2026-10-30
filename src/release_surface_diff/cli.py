"""Command line entry point: run, extract, watch."""

import argparse
import sys
from pathlib import Path

from . import config, gitops
from .records import record_sort_key

EXIT_NO_DELTA = 0
EXIT_DELTA = 1
EXIT_ERROR = 2

RUN_EXIT_HELP = (
    "exit codes: 0 no surface or guard delta, 1 a delta was found, 2 tool error"
)
EXTRACT_EXIT_HELP = (
    "exit codes: 0 records emitted as JSONL (possibly none), 2 tool error"
)
WATCH_EXIT_HELP = (
    "exit codes: 0 the check completed, 2 a target run failed or config invalid"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release-surface-diff",
        description=(
            "Diff the externally reachable surface of a repo between two "
            "release tags: entry points added, removed, changed, or renamed, "
            "plus the security-relevant hunks between them."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run", help="diff two tags of a repo", epilog=RUN_EXIT_HELP,
    )
    run.add_argument("--repo", required=True, help="local path or git URL of the target repo")
    run.add_argument("--from", dest="from_tag", metavar="TAG", help="older tag")
    run.add_argument("--to", dest="to_tag", metavar="TAG", help="newer tag")
    run.add_argument(
        "--latest", action="store_true",
        help="diff the newest tag against the prior tag in semver order",
    )
    run.add_argument(
        "--pre", action="store_true",
        help="with --latest: include pre-release tags in the ordering",
    )
    run.add_argument(
        "--lang", action="append", metavar="LANG[,LANG...]",
        help="languages to extract (go, rust, python, ts); default: autodetect "
             "from manifests in the to-side tree",
    )
    run.add_argument(
        "--include", action="append", default=[], metavar="GLOB",
        help="repo-relative path globs to keep (repeatable; default: all)",
    )
    run.add_argument(
        "--exclude", action="append", default=None, metavar="GLOB",
        help="repo-relative path globs to drop (repeatable; default: tests, "
             "vendored, generated, docs)",
    )
    run.add_argument("--out", default="out", help="output directory (default: out/)")

    extract = sub.add_parser(
        "extract",
        help="print one tree's normalized record inventory as JSONL",
        epilog=EXTRACT_EXIT_HELP,
    )
    extract.add_argument("--root", required=True, help="tree to extract")
    extract.add_argument("--lang", required=True, help="language to extract")
    extract.add_argument(
        "--include", action="append", default=[], metavar="GLOB",
        help="repo-relative path globs to keep (repeatable; default: all)",
    )
    extract.add_argument(
        "--exclude", action="append", default=None, metavar="GLOB",
        help="repo-relative path globs to drop (repeatable)",
    )

    watch = sub.add_parser(
        "watch",
        help="one-shot check of targets.toml for new tags",
        epilog=WATCH_EXIT_HELP,
    )
    watch.add_argument("--config", required=True, help="targets.toml path")

    return parser


def _extract_stub(root: Path, lang: str, matcher: config.PathMatcher) -> list:
    """Placeholder for the extractor dispatch that lands in extract.py (U2).

    Returns an empty inventory for now; callers surface the gap as a warning
    per R17 rather than a silent skip.
    """
    return []


def _matcher(args) -> config.PathMatcher:
    # --exclude globs add to the built-in defaults rather than replacing them:
    # a security tool should not re-include tests/vendor because a caller
    # passed one extra pattern.
    return config.PathMatcher(
        include=args.include,
        exclude=tuple(config.DEFAULT_EXCLUDE) + tuple(args.exclude or ()),
    )


def _cmd_run(args: argparse.Namespace) -> int:
    if args.latest and (args.from_tag or args.to_tag):
        print("error: --latest cannot be combined with --from/--to", file=sys.stderr)
        return EXIT_ERROR
    if not args.latest and not (args.from_tag and args.to_tag):
        print("error: run needs --from and --to, or --latest", file=sys.stderr)
        return EXIT_ERROR
    if args.pre and not args.latest:
        print("error: --pre only applies with --latest", file=sys.stderr)
        return EXIT_ERROR
    langs = config.parse_langs(args.lang)
    matcher = _matcher(args)
    with gitops.open_repo(args.repo) as repo:
        if args.latest:
            from_tag, to_tag = gitops.resolve_latest(repo.path, pre=args.pre)
        else:
            from_tag, to_tag = gitops.resolve_pair(
                repo.path, args.from_tag, args.to_tag
            )
        with gitops.worktrees(repo.path, [from_tag, to_tag]) as wts:
            diff = gitops.git_diff(repo.path, from_tag, to_tag)
            langs = langs or config.detect_languages(wts[to_tag])
            records_from = sum(
                (_extract_stub(wts[from_tag], lang, matcher) for lang in langs), []
            )
            records_to = sum(
                (_extract_stub(wts[to_tag], lang, matcher) for lang in langs), []
            )
    changed_files = diff.count("diff --git ")
    print(f"{from_tag}..{to_tag}: {changed_files} file(s) changed")
    if not records_from and not records_to:
        print(
            "note: no extractors wired yet; run covered tag resolution, "
            "worktrees, and diff capture only",
            file=sys.stderr,
        )
    # Until the record diff and guard classifier land (U3, U6), the captured
    # diff is the delta signal: empty means nothing changed between the tags.
    return EXIT_DELTA if diff.strip() else EXIT_NO_DELTA


def _cmd_extract(args: argparse.Namespace) -> int:
    root = Path(args.root)
    if not root.is_dir():
        print(f"error: --root is not a directory: {args.root}", file=sys.stderr)
        return EXIT_ERROR
    try:
        langs = config.parse_langs(args.lang)
    except config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not langs:
        print("error: --lang needs a language (go, rust, python, ts)", file=sys.stderr)
        return EXIT_ERROR
    matcher = _matcher(args)
    records = _extract_stub(root, langs[0], matcher)
    for rec in sorted(records, key=record_sort_key):
        print(rec.to_json())
    if not records:
        print(
            f"note: no extractor wired for '{langs[0]}' yet; empty inventory",
            file=sys.stderr,
        )
    return EXIT_NO_DELTA


def _cmd_watch(args: argparse.Namespace) -> int:
    try:
        config.load_targets(args.config)
    except config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print("watch is not implemented yet (lands in U9)", file=sys.stderr)
    return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "extract":
            return _cmd_extract(args)
        if args.command == "watch":
            return _cmd_watch(args)
        print(f"{args.command} is not implemented yet", file=sys.stderr)
        return EXIT_ERROR
    except (gitops.GitError, config.ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
