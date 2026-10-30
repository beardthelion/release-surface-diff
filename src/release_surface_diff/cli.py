"""Command line entry point: run, extract, watch."""

import argparse
import sys
from pathlib import Path

from . import config, diff, extract, gitops, guards, rank, report, seeds
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
    _tool_flags(run)

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
    _tool_flags(extract)

    watch = sub.add_parser(
        "watch",
        help="one-shot check of targets.toml for new tags",
        epilog=WATCH_EXIT_HELP,
    )
    watch.add_argument("--config", required=True, help="targets.toml path")

    return parser


def _tool_flags(sub) -> None:
    """Explicit-path overrides for the extractor tools (discovery: flag >
    env var > PATH, KTD3)."""
    sub.add_argument("--egress-plane", metavar="PATH",
                     help="path to the egress-plane binary")
    sub.add_argument("--acl-guard-audit", metavar="PATH",
                     help="path to the acl-guard-audit binary")
    sub.add_argument("--go-extractor", metavar="PATH",
                     help="path to the go-extractor helper binary")
    sub.add_argument("--rust-extractor", metavar="PATH",
                     help="path to the rust-extractor helper binary")


def _tools(args) -> extract.ToolPaths:
    return extract.ToolPaths(
        egress_plane=getattr(args, "egress_plane", None),
        acl_guard_audit=getattr(args, "acl_guard_audit", None),
        go_extractor=getattr(args, "go_extractor", None),
        rust_extractor=getattr(args, "rust_extractor", None),
    )


def _report_result(tag: str, result) -> None:
    """Warnings and gap notes go to stderr; stdout stays machine output."""
    for w in result.warnings:
        print(f"warning [{tag}]: {w}", file=sys.stderr)
    for note in result.gap_notes:
        print(f"note [{tag}]: {note}", file=sys.stderr)


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
    tools = _tools(args)
    slug = gitops.repo_slug(args.repo)
    with gitops.open_repo(args.repo) as repo:
        if args.latest:
            from_tag, to_tag = gitops.resolve_latest(repo.path, pre=args.pre)
        else:
            from_tag, to_tag = gitops.resolve_pair(
                repo.path, args.from_tag, args.to_tag
            )
        diff_text = gitops.git_diff(repo.path, from_tag, to_tag)
        with gitops.worktrees(repo.path, [from_tag, to_tag]) as wts:
            langs = langs or config.detect_languages(wts[to_tag])
            result_from = extract.extract_tree(
                wts[from_tag], langs, matcher, tools
            )
            result_to = extract.extract_tree(
                wts[to_tag], langs, matcher, tools
            )
            snap_from = diff.Snapshot(
                result_from.records,
                result_from.symbols,
                _reader(wts[from_tag]),
                low_confidence=not result_from.complete,
            )
            snap_to = diff.Snapshot(
                result_to.records,
                result_to.symbols,
                _reader(wts[to_tag]),
                low_confidence=not result_to.complete,
            )
            surface = diff.diff_snapshots(
                snap_from, snap_to,
                repo=slug, from_ref=from_tag, to_ref=to_tag,
            )
            symbols = result_from.symbols + result_to.symbols
            edges = result_from.edges + result_to.edges
        hunks = guards.classify_diff(diff_text, matcher, symbols)
        ranked = rank.rank_hunks(hunks, surface, edges)
        seed_rows = seeds.build_seeds(
            repo.path, args.repo, hunks, from_tag, to_tag
        )
        guards_doc = guards.guards_json(
            hunks, repo=slug, from_ref=from_tag, to_ref=to_tag
        )
        generated_at = surface["generated_at"]
        gap_notes = result_from.gap_notes + result_to.gap_notes
        report_text = report.build_report(
            surface, ranked, gap_notes,
            repo=slug, from_ref=from_tag, to_ref=to_tag,
            generated_at=generated_at,
        )
    _report_result(from_tag, result_from)
    _report_result(to_tag, result_to)
    out_dir = Path(args.out) / slug / f"{from_tag}..{to_tag}"
    diff.write_surface_json(surface, out_dir / "surface.json")
    guards.write_guards_json(guards_doc, out_dir / "guards.json")
    seeds.write_seeds_jsonl(seed_rows, out_dir / "seeds.jsonl")
    report.write_report(report_text, out_dir / "report.md")
    print(f"{from_tag}..{to_tag}: outputs in {out_dir}")
    has_delta = bool(
        surface["added"] or surface["removed"] or surface["changed"]
        or surface["renamed"] or hunks
    )
    return EXIT_DELTA if has_delta else EXIT_NO_DELTA


def _reader(root):
    """Symbol-table body lookup: repo-relative path -> file text (KTD7)."""
    root = Path(root)

    def read(path):
        try:
            return (root / path).read_text(errors="replace")
        except OSError:
            return None

    return read


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
    result = extract.extract_lang(root, langs[0], matcher, _tools(args))
    for rec in sorted(result.records, key=record_sort_key):
        print(rec.to_json())
    _report_result(langs[0], result)
    if not result.records:
        print(f"note: empty inventory for '{langs[0]}'", file=sys.stderr)
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
