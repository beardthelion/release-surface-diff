"""Command line entry point: run, extract, watch."""

import argparse
import sys
from pathlib import Path

from . import config, diff, extract, gitops, guards, rank, report, seeds
from ._util import utc_now
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


def _matcher(include=(), exclude=None) -> config.PathMatcher:
    # --exclude globs add to the built-in defaults rather than replacing them:
    # a security tool should not re-include tests/vendor because a caller
    # passed one extra pattern.
    return config.PathMatcher(
        include=include,
        exclude=tuple(config.DEFAULT_EXCLUDE) + tuple(exclude or ()),
    )


def run_pair(
    repo_arg,
    repo_path,
    from_tag: str,
    to_tag: str,
    *,
    langs=(),
    include=(),
    exclude=None,
    out="out",
    tools=None,
) -> tuple[int, bool]:
    """Diff one tag pair of an already-open repo and write the outputs.

    The shared pipeline behind `run` (explicit pair or --latest) and
    `watch` (KTD9): repo_arg is the user-supplied --repo value whose slug
    names the outputs and the seeds `repo` field; repo_path is the local
    checkout git queries run against. Returns (exit_code, degraded): exit
    0 for no delta, 1 for a delta; degraded marks a side that extracted
    with a missing or partial tool, so the delta is low-confidence and
    watch must not advance last_tag past this pair.
    """
    matcher = _matcher(include, exclude)
    tools = tools or extract.ToolPaths()
    slug = gitops.repo_slug(repo_arg)
    if not gitops.is_url(str(repo_arg)):
        origin = gitops.origin_url(repo_arg)
        if origin:
            slug = gitops.repo_slug(origin)
    generated_at = utc_now()
    diff_text = gitops.git_diff(repo_path, from_tag, to_tag)
    with gitops.worktrees(repo_path, [from_tag, to_tag]) as wts:
        langs = langs or config.detect_languages(wts[to_tag])
        if not langs:
            print(
                "warning: no supported-language manifests found at tree "
                "root; pass --lang",
                file=sys.stderr,
            )
        result_from = extract.extract_tree(
            wts[from_tag], langs, matcher, tools
        )
        result_to = extract.extract_tree(
            wts[to_tag], langs, matcher, tools
        )
        if not langs:
            gap = (
                "no supported-language manifests found at tree root; "
                "pass --lang"
            )
            result_from.gap_notes.append(gap)
            result_to.gap_notes.append(gap)
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
            generated_at=generated_at,
        )
        edges = result_from.edges + result_to.edges
    hunks = guards.classify_diff(
        diff_text, matcher, result_to.symbols,
        symbols_from=result_from.symbols, langs=set(langs),
    )
    ranked = rank.rank_hunks(hunks, surface, edges)
    seed_rows = seeds.build_seeds(
        repo_path, repo_arg, hunks, from_tag, to_tag
    )
    guards_doc = guards.guards_json(
        hunks, repo=slug, from_ref=from_tag, to_ref=to_tag,
        generated_at=generated_at,
    )
    gap_notes = result_from.gap_notes + result_to.gap_notes
    report_text = report.build_report(
        surface, ranked, gap_notes,
        repo=slug, from_ref=from_tag, to_ref=to_tag,
        generated_at=generated_at,
    )
    _report_result(from_tag, result_from)
    _report_result(to_tag, result_to)
    out_dir = Path(out) / gitops.safe_slug(slug) / f"{from_tag}..{to_tag}"
    diff.write_surface_json(surface, out_dir / "surface.json")
    guards.write_guards_json(guards_doc, out_dir / "guards.json")
    seeds.write_seeds_jsonl(seed_rows, out_dir / "seeds.jsonl")
    report.write_report(report_text, out_dir / "report.md")
    print(f"{from_tag}..{to_tag}: outputs in {out_dir}")
    has_delta = bool(
        surface["added"] or surface["removed"] or surface["changed"]
        or surface["renamed"] or hunks
    )
    # An empty lang set means autodetect found no manifest: the run is
    # degraded so watch does not advance last_tag past this pair.
    degraded = (
        not langs
        or not (result_from.complete and result_to.complete)
    )
    return (EXIT_DELTA if has_delta else EXIT_NO_DELTA), degraded


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
    tools = _tools(args)
    with gitops.open_repo(args.repo) as repo:
        if args.latest:
            from_tag, to_tag = gitops.resolve_latest(repo.path, pre=args.pre)
        else:
            from_tag, to_tag = gitops.resolve_pair(
                repo.path, args.from_tag, args.to_tag
            )
        code, _degraded = run_pair(
            args.repo, repo.path, from_tag, to_tag,
            langs=langs, include=args.include, exclude=args.exclude,
            out=args.out, tools=tools,
        )
    return code


def _reader(root):
    """Symbol-table body lookup: repo-relative path -> file text (KTD7).

    Reads that would escape the worktree root (a symlinked or `..` record
    path in a body_diff lookup) are refused.
    """
    root = Path(root).resolve()

    def read(path):
        try:
            p = (root / path).resolve()
            if not p.is_relative_to(root):
                return None
            return p.read_text(errors="replace")
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
    if len(langs) != 1:
        print("error: extract takes exactly one --lang", file=sys.stderr)
        return EXIT_ERROR
    matcher = _matcher(args.include, args.exclude)
    result = extract.extract_lang(root, langs[0], matcher, _tools(args))
    for rec in sorted(result.records, key=record_sort_key):
        print(rec.to_json())
    _report_result(langs[0], result)
    if not result.records:
        print(f"note: empty inventory for '{langs[0]}'", file=sys.stderr)
    if not result.complete and not result.records:
        return EXIT_ERROR
    return EXIT_NO_DELTA


def _cmd_watch(args: argparse.Namespace) -> int:
    from . import watch

    return watch.check(args.config)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "extract":
            return _cmd_extract(args)
        if args.command == "watch":
            return _cmd_watch(args)
    except (gitops.GitError, config.ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        # Anything unexpected is still a tool error; leaking the
        # interpreter's exit 1 would collide with the delta-found code.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
