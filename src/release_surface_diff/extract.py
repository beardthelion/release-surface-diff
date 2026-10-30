"""Per-language extraction dispatch (R5, R6, KTD3, KTD11).

- every language's outbound surface comes from the egress-plane adapter (R5)
- python/ts route surface comes from acl-guard-audit plus the in-repo route
  extractor (KD1/KTD12; the module lands in U5, so its call site tolerates
  absence)
- go/rust records come from the helper binaries via adapters/helpers.py

Discovery per external tool is explicit flag > env var > PATH, then a
degrade warning plus a gap note (KTD3, R17). include/exclude filtering is
post-hoc on record `file` via PathMatcher (KTD11).
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .adapters import AdapterResult, aclguard, egress, helpers
from .config import PathMatcher

ACLGUARD_LANGS = ("python", "ts")


@dataclass
class ToolPaths:
    """Explicit tool-path overrides (the CLI's --*-plane/--*-extractor flags)."""

    egress_plane: str | None = None
    acl_guard_audit: str | None = None
    go_extractor: str | None = None
    rust_extractor: str | None = None


def find_tool(binary: str, explicit=None, env_var=None):
    """Resolve one external tool: flag > env var > PATH (KTD3).

    Returns (path, None) or (None, reason). A configured path that is not an
    executable file does not fall back to PATH: the caller asked for a
    specific binary, and silently substituting another hides the mistake.
    """
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit, None
        return None, f"explicit path {explicit} is not an executable file"
    env = os.environ.get(env_var) if env_var else None
    if env:
        if os.path.isfile(env) and os.access(env, os.X_OK):
            return env, None
        return None, f"{env_var}={env} is not an executable file"
    found = shutil.which(binary)
    if found:
        return found, None
    return (
        None,
        f"not found (set {env_var} or the matching flag, or put {binary} on PATH)",
    )


def _py_ts_routes(root: Path) -> AdapterResult:
    """In-repo Python/TS route extractor (KD1/KTD12), landed in U5.

    The call site is wired now; until the module exists the acl-guard-audit
    blind spot (sink-free handlers, including most paramless routes) is
    reported as a gap instead of silently accepted. U5's entry point is
    `extractors.py_ts_routes.extract(root) -> AdapterResult | list[Record]`.
    """
    try:
        from .extractors import py_ts_routes
    except ImportError:
        return AdapterResult().degrade(
            "py-ts-routes",
            "in-repo Python/TS route extractor not present (lands in U5); "
            "acl-guard-audit sees only sink-bearing handlers and id-param "
            "candidates (KD1)",
        )
    try:
        out = py_ts_routes.extract(root)
    except Exception as exc:
        return AdapterResult().degrade("py-ts-routes", f"extract failed: {exc}")
    if isinstance(out, AdapterResult):
        return out
    result = AdapterResult()
    if out:
        result.records = list(out)
    return result


def _protocol_ids(root: Path) -> AdapterResult:
    """Bare-literal protocol-id pass (R10): the single producer for
    req/resp protocol strings and gossip topic templates in every
    language. Same tolerate-absence shape as _py_ts_routes."""
    try:
        from .extractors import protocol_ids
    except ImportError:
        return AdapterResult().degrade(
            "protocol-ids", "in-repo protocol-id extractor not present"
        )
    try:
        out = protocol_ids.extract(root)
    except Exception as exc:
        return AdapterResult().degrade("protocol-ids", f"extract failed: {exc}")
    if isinstance(out, AdapterResult):
        return out
    result = AdapterResult()
    if out:
        result.records = list(out)
    return result


def _dedupe_py_ts_routes(result: AdapterResult) -> None:
    """One entry point -> one record (KD1): acl-guard-audit and the in-repo
    extractor describe the same routes, so a (kind, id) pair seen by both
    keeps the in-repo record -- its attrs carry the auth_hint middleware
    chain and it is guaranteed a symbol-table span (KTD7)."""
    ours = {
        (r.id, r.lang)
        for r in result.records
        if r.kind == "route" and r.extractor == "py-ts-routes"
    }
    if not ours:
        return
    result.records = [
        r
        for r in result.records
        if not (
            r.kind == "route"
            and r.extractor != "py-ts-routes"
            and (r.id, r.lang) in ours
        )
    ]


def extract_tree(root, langs, matcher=None, tools=None) -> AdapterResult:
    """Run every adapter that covers `langs` against one tree.

    Each tool runs at most once; its records are kept only when their `lang`
    was requested and their `file` passes the matcher (KTD11).
    """
    root = Path(root)
    langs = tuple(dict.fromkeys(langs or ()))
    tools = tools or ToolPaths()
    matcher = matcher or PathMatcher()
    result = AdapterResult()

    if langs:
        exe, warn = find_tool(
            egress.TOOL_NAME, tools.egress_plane, egress.ENV_VAR
        )
        result.merge(
            egress.scan(root, exe) if exe else AdapterResult().degrade(egress.TOOL_NAME, warn)
        )

    if set(langs) & set(ACLGUARD_LANGS):
        exe, warn = find_tool(
            aclguard.TOOL_NAME, tools.acl_guard_audit, aclguard.ENV_VAR
        )
        result.merge(
            aclguard.scan(root, exe)
            if exe
            else AdapterResult().degrade(aclguard.TOOL_NAME, warn)
        )
        result.merge(_py_ts_routes(root))
        _dedupe_py_ts_routes(result)

    if langs:
        result.merge(_protocol_ids(root))

    for lang in langs:
        if lang in helpers.HELPERS:
            explicit = getattr(tools, f"{lang}_extractor", None)
            result.merge(helpers.run_helper(lang, root, explicit=explicit))

    # Post-hoc filter (KTD11): black-box tools report what they saw; the
    # matcher decides what is in scope.
    result.records = [
        rec for rec in result.records if rec.lang in langs and matcher.matches(rec.file)
    ]
    return result


def extract_lang(root, lang, matcher=None, tools=None) -> AdapterResult:
    """Convenience for the `extract` command's single --lang."""
    return extract_tree(root, (lang,), matcher=matcher, tools=tools)
