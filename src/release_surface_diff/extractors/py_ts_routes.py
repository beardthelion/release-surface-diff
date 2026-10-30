"""In-repo Python/TS HTTP-route extractor (R6, R7; KD1, KTD12).

acl-guard-audit inventories only handlers that reach a recognized sink,
plus id-param candidates: sink-free handlers -- including most paramless
routes -- are invisible to it. This module closes that blind spot with
syntactic route discovery:

- Python via ``ast``: ``app|router.{verb}`` decorators and calls, Flask
  ``route``/``add_url_rule``, FastAPI ``api_route``, Django
  ``path()``/``re_path()``/``url()``, and ``include_router`` /
  ``register_blueprint`` / ``include()`` prefixes joined where resolvable.
- TS/JS via pattern rules (no TS parser in this package):
  ``app|router.{verb}(`` and ``.route(...)`` chains, Fastify ``{method,
  url}`` objects, and the Next.js ``app/**/route.*`` / ``pages/api/**``
  file conventions. ``.use(...)`` middleware names are recorded into
  ``attrs.auth_hint`` where syntactically visible.

Template syntaxes (``:id``, ``<int:id>``, ``[id]``, ``{id:int}``) all
canonicalize to ``{name}`` -- the same normal form acl-guard-audit emits,
so records from the two extractors dedupe on ``(kind, id)``. The
normalization helpers below mirror ``acl_guard_audit/routes.py`` and are
vendored, not imported: this package must not depend on the sibling tool.

Entry point: ``extract(root) -> AdapterResult`` (consumed by
``extract._py_ts_routes``).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from ..adapters import AdapterResult
from ..config import PathMatcher
from ..records import Record, SymbolSpan, route_id

EXTRACTOR = "py-ts-routes"

HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")

_TS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_PY_EXT = ".py"


# ---- template normalization (vendored from acl_guard_audit/routes.py) ----

_EXPRESS = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")
_ANGLE = re.compile(r"<(?:[A-Za-z_][A-Za-z0-9_]*:)?([A-Za-z_][A-Za-z0-9_]*)>")
_BRACE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}")
_NEXT_SEGMENT = re.compile(r"^\[(?:\.\.\.)?([A-Za-z_][A-Za-z0-9_]*)\]$")
_DJANGO_REGEX_GROUP = re.compile(r"\(\?P<([A-Za-z_][A-Za-z0-9_]*)>[^)]*\)")


def normalize_template(raw: str) -> str:
    """Rewrite any supported template syntax to the canonical ``{name}`` form."""
    path = raw.strip()
    path = _DJANGO_REGEX_GROUP.sub(lambda m: "{" + m.group(1) + "}", path)
    path = path.replace("\\/", "/")
    path = _BRACE.sub(lambda m: "{" + m.group(1) + "}", path)
    path = _ANGLE.sub(lambda m: "{" + m.group(1) + "}", path)
    # Express parameters. Run after brace/angle so ":int" inside <...> is untouched.
    path = _EXPRESS.sub(lambda m: "{" + m.group(1) + "}", path)
    path = path.lstrip("^").rstrip("$")
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def canonical_template(route: str) -> str:
    """``normalize_template`` plus Next ``[id]``/``[...rest]`` segments."""
    raw = [
        "{" + m.group(1) + "}" if (m := _NEXT_SEGMENT.match(seg)) else seg
        for seg in route.strip().split("/")
    ]
    return normalize_template("/".join(raw))


def join_prefix(prefix: str, route: str) -> str:
    """Join a mount prefix onto a route: ``/api/v1`` + ``/{id}``."""
    if not prefix:
        return route
    if not route or route == "/":
        return prefix
    return prefix.rstrip("/") + "/" + route.lstrip("/")


def next_route_from_path(relpath: str) -> str | None:
    """Derive the route of a Next.js route file from its repo-relative path."""
    parts = relpath.replace("\\", "/").split("/")
    base = parts[-1]
    stem = re.sub(r"\.(tsx?|jsx?|mjs|cjs)$", "", base)
    if stem == base:
        return None  # not a script file
    if "app" in parts and stem == "route":
        idx = len(parts) - 1 - parts[::-1].index("app")
        segments = parts[idx + 1 : -1]
    elif "pages" in parts and "api" in parts[parts.index("pages") :]:
        idx = len(parts) - 1 - parts[::-1].index("pages")
        segments = parts[idx + 1 : -1]
        if stem != "index":
            segments = segments + [stem]
    else:
        return None
    out = []
    for seg in segments:
        if seg.startswith("(") and seg.endswith(")"):
            continue  # route group, not part of the URL
        m = _NEXT_SEGMENT.match(seg)
        out.append("{" + m.group(1) + "}" if m else seg)
    return "/" + "/".join(out)


# ---- python pass (ast) ----


def _tail(func) -> str:
    """Last component of a callee: ``app.get`` -> ``get`` (mirrors
    ``acl_guard_audit/routes.py::_tail``)."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _str_val(node) -> str:
    return (
        node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        else ""
    )


def _kw_str(call, name: str) -> str:
    for kw in call.keywords:
        if (
            kw.arg == name
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            return kw.value.value
    return ""


def _kw_methods(call) -> list[str]:
    """``methods=["GET", "POST"]`` / ``methods=("GET",)`` -> lowercase verbs."""
    for kw in call.keywords:
        if kw.arg != "methods":
            continue
        v = kw.value
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            seq = [v.value]
        elif isinstance(v, (ast.List, ast.Tuple, ast.Set)):
            seq = [_str_val(e) for e in v.elts]
        else:
            continue
        return [s.lower() for s in seq if s and s.lower() in HTTP_VERBS]
    return []


def _dotted(node) -> str:
    """``views.detail`` style dotted name for a Name/Attribute chain."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(parts[::-1])


def _callee_name(node) -> str:
    """Symbol name for a view/handler argument: name tail or class of
    ``X.as_view()``."""
    if isinstance(node, ast.Call):
        if _tail(node.func) == "as_view" and isinstance(
            node.func, ast.Attribute
        ):
            tail = _dotted(node.func.value).rsplit(".", 1)[-1]
            return tail
        return ""
    name = _dotted(node)
    return name.rsplit(".", 1)[-1] if name else ""


def _module_of(node, imports: dict) -> str:
    """Module spec a mount argument resolves to, via the file's imports."""
    if isinstance(node, ast.Name):
        return imports.get(node.id, "")
    if isinstance(node, ast.Attribute):
        root = node
        parts = []
        while isinstance(root, ast.Attribute):
            parts.append(root.attr)
            root = root.value
        if isinstance(root, ast.Name):
            base = imports.get(root.id, root.id)
            return ".".join([base] + parts[::-1])
    return ""


def _build_prefix_index(root: Path, matcher: PathMatcher) -> dict:
    """rel path -> literal route prefix, best-effort.

    Mirrors ``acl_guard_audit/routes.py::build_prefix_index``: same-file
    ``APIRouter(prefix=...)``/``Blueprint(url_prefix=...)`` plus cross-file
    ``include_router(..., prefix=)``, ``register_blueprint(...,
    url_prefix=)`` and Django ``path("p/", include(...))`` resolved through
    the mounting file's imports. Literal strings only; dynamic mounts are
    skipped and routes keep their decorator-local form.
    """
    index: dict[str, str] = {}
    mounts: list[tuple[str, str]] = []
    for py in sorted(root.rglob("*" + _PY_EXT)):
        rel = py.relative_to(root).as_posix()
        if not matcher.matches(rel):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        imports: dict[str, str] = {}
        own_prefix = ""
        var_prefixes: dict[str, str] = {}
        pkg_parts = rel.split("/")[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if node.level and len(pkg_parts) >= node.level - 1:
                    base = ".".join(pkg_parts[: len(pkg_parts) - node.level + 1])
                    mod = f"{base}.{mod}" if mod else base
                if not mod:
                    continue
                for a in node.names:
                    imports[a.asname or a.name] = f"{mod}.{a.name}"
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imports[a.asname or a.name.split(".")[-1]] = a.name
            elif isinstance(node, ast.Assign):
                v = node.value
                if isinstance(v, ast.Call) and _tail(v.func) in (
                    "APIRouter",
                    "Blueprint",
                ):
                    p = _kw_str(v, "prefix") or _kw_str(v, "url_prefix")
                    own_prefix = p or own_prefix
                    if p:
                        for t in node.targets:
                            if isinstance(t, ast.Name):
                                var_prefixes[t.id] = p
            elif isinstance(node, ast.Call):
                f = _tail(node.func)
                if f in ("include_router", "register_blueprint") and node.args:
                    p = _kw_str(node, "prefix") or _kw_str(node, "url_prefix")
                    recv = (
                        node.func.value.id
                        if isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        else ""
                    )
                    if p or var_prefixes.get(recv):
                        p = join_prefix(var_prefixes.get(recv, ""), p)
                    mod = _module_of(node.args[0], imports)
                    if p and mod:
                        mounts.append((mod, p))
                elif f in ("path", "url", "re_path") and len(node.args) >= 2:
                    p = _str_val(node.args[0])
                    arg = node.args[1]
                    mod = ""
                    if (
                        isinstance(arg, ast.Call)
                        and _tail(arg.func) == "include"
                        and arg.args
                    ):
                        inc = arg.args[0]
                        mod = (
                            inc.value
                            if isinstance(inc, ast.Constant)
                            and isinstance(inc.value, str)
                            else _module_of(inc, imports)
                        )
                    if p and mod:
                        mounts.append((mod, p))
        if own_prefix:
            index[rel] = (
                join_prefix(own_prefix, index.get(rel, "/"))
                if index.get(rel)
                else own_prefix
            )
    for mod, p in mounts:
        specs = [mod] + ([mod.rsplit(".", 1)[0]] if "." in mod else [])
        for spec in specs:
            relpath = spec.replace(".", "/")
            applied = False
            for cand in (
                relpath + ".py",
                relpath + "/urls.py",
                relpath + "/__init__.py",
            ):
                if (root / cand).exists():
                    index[cand] = join_prefix(p, index.get(cand, "/"))
                    applied = True
                    break
            if applied:
                break
            mod_dir = root / relpath
            if mod_dir.is_dir():
                for f in sorted(mod_dir.rglob("*.py")):
                    crelp = f.relative_to(root).as_posix()
                    index[crelp] = join_prefix(p, index.get(crelp, "/"))
                break
    return index


def _enclosing_function(node, parents: dict):
    fn = node
    while fn is not None and not isinstance(
        fn, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        fn = parents.get(id(fn))
    return fn


def _emit(result: AdapterResult, *, rel, method, raw_template, symbol, line,
          prefix, lang, attrs=None):
    template = normalize_template(join_prefix(prefix, raw_template))
    result.records.append(
        Record(
            kind="route",
            id=route_id(method, template),
            file=rel,
            line=line,
            symbol=symbol or "",
            lang=lang,
            extractor=EXTRACTOR,
            attrs=attrs or {},
        )
    )


def _py_decorator_routes(result, rel, fn, dec, prefix):
    """Route-shaped decorator on ``fn``: ``@app.get``/``@router.post``/
    ``@app.route``/``@router.api_route``."""
    call = dec if isinstance(dec, ast.Call) else None
    if call is None or not call.args:
        return
    tail = _tail(call.func).lower()
    if tail not in HTTP_VERBS and tail not in ("route", "api_route"):
        return
    raw = _str_val(call.args[0])
    if not raw:
        return
    if tail in HTTP_VERBS:
        methods = [tail]
    else:
        methods = _kw_methods(call) or [None]
    for method in methods:
        _emit(
            result,
            rel=rel,
            method=method,
            raw_template=raw,
            symbol=fn.name,
            line=dec.lineno,
            prefix=prefix,
            lang="python",
        )


def _py_call_routes(result, rel, node, prefix, parents):
    """Call-statement registrations: ``add_url_rule`` and Django
    ``path``/``re_path``/``url``."""
    tail = _tail(node.func)
    if tail == "add_url_rule":
        if not node.args:
            return
        raw = _str_val(node.args[0])
        if not raw:
            return
        symbol = ""
        for kw in node.keywords:
            if kw.arg == "view_func":
                symbol = _callee_name(kw.value)
        if not symbol:
            for arg in node.args[1:]:
                symbol = _callee_name(arg)
                if symbol:
                    break
        if not symbol:
            fn = _enclosing_function(node, parents)
            symbol = fn.name if fn else ""
        methods = _kw_methods(node) or [None]
        for method in methods:
            _emit(
                result,
                rel=rel,
                method=method,
                raw_template=raw,
                symbol=symbol,
                line=node.lineno,
                prefix=prefix,
                lang="python",
            )
    elif tail in ("path", "re_path", "url"):
        # django.urls.path is imported bare (`path(...)`) or via the urls
        # module; an attribute call on an arbitrary object is not Django
        if not isinstance(node.func, ast.Name):
            recv = _dotted(node.func.value) if isinstance(node.func, ast.Attribute) else ""
            if not recv.endswith("urls"):
                return
        if not node.args:
            return
        raw = _str_val(node.args[0])
        if not raw:
            return
        if len(node.args) >= 2:
            second = node.args[1]
            if isinstance(second, ast.Call) and _tail(second.func) == "include":
                return  # mount point, not a route
            symbol = _callee_name(second)
        else:
            symbol = ""
        _emit(
            result,
            rel=rel,
            method=None,
            raw_template=raw,
            symbol=symbol,
            line=node.lineno,
            prefix=prefix,
            lang="python",
        )


def _extract_python(root: Path, matcher: PathMatcher, result: AdapterResult):
    prefixes = _build_prefix_index(root, matcher)
    for py in sorted(root.rglob("*" + _PY_EXT)):
        rel = py.relative_to(root).as_posix()
        if not matcher.matches(rel):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError) as exc:
            result.warnings.append(
                f"{EXTRACTOR}: skipped unparseable file {rel}: {exc}"
            )
            continue
        except OSError as exc:
            result.warnings.append(f"{EXTRACTOR}: unreadable file {rel}: {exc}")
            continue
        prefix = prefixes.get(rel, "")
        parents: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                result.symbols.append(
                    SymbolSpan(
                        file=rel,
                        symbol=node.name,
                        start_line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                    )
                )
                for dec in node.decorator_list:
                    _py_decorator_routes(result, rel, node, dec, prefix)
            elif isinstance(node, ast.ClassDef):
                result.symbols.append(
                    SymbolSpan(
                        file=rel,
                        symbol=node.name,
                        start_line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                    )
                )
            elif isinstance(node, ast.Call):
                _py_call_routes(result, rel, node, prefix, parents)


# ---- TS/JS pass (pattern rules, no parser) ----

# Receivers seen in real apps: express apps/routers, fastify instances,
# koa/hono-style `api`, nested groups. `all` maps to ANY.
_TS_RECEIVERS = frozenset(
    {
        "app",
        "router",
        "routers",
        "server",
        "api",
        "fastify",
        "route",
        "group",
        "sub",
    }
)

_TS_VERB_RE = re.compile(
    r"\b([A-Za-z_$][\w$]*)\s*\.\s*"
    r"(get|post|put|patch|delete|head|options|all)\s*"
    r"\(\s*(['\"`])(/[^'\"`]*)\3"
)
_TS_ROUTE_CHAIN_RE = re.compile(
    r"\.\s*route\s*\(\s*(['\"`])(/[^'\"`]*)\1\s*\)"
)
_TS_CHAIN_VERB_RE = re.compile(
    r"\.\s*(get|post|put|patch|delete|head|options|all)\s*\("
)
_TS_USE_RE = re.compile(
    r"\b([A-Za-z_$][\w$]*)\s*\.\s*use\s*\("
)
_TS_ROUTER_VAR_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
    r"(?:[A-Za-z_$][\w$]*\s*\.\s*)*Router\s*\("
)
_TS_STRING_RE = re.compile(r"(['\"`])([^'\"`]*)\1")
_NEXT_EXPORT_VERB_RE = re.compile(
    r"\bexport\s+(?:async\s+)?function\s+"
    r"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b|"
    r"\bexport\s+(?:const|let)\s+"
    r"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b"
)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _brace_span(text: str, open_pos: int, open_c: str, close_c: str):
    """(start, end) span of the bracket group opened at ``open_pos``."""
    depth = 0
    i = open_pos
    in_str = None
    while i < len(text):
        c = text[i]
        if in_str:
            if c == in_str and text[i - 1] != "\\":
                in_str = None
        elif c in "'\"`":
            in_str = c
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return open_pos, i
        i += 1
    return None


def _func_span(text: str, name: str):
    """Best-effort (start_line, end_line) for a named TS function."""
    if not name:
        return None
    pat = re.compile(
        r"\bfunction\s+" + re.escape(name) + r"\s*\(|"
        r"\b(?:const|let|var)\s+" + re.escape(name) + r"\s*="
    )
    m = pat.search(text)
    if not m:
        return None
    b = text.find("{", m.end())
    if b == -1:
        return None
    span = _brace_span(text, b, "{", "}")
    if not span:
        return None
    return _line_of(text, m.start()), _line_of(text, span[1])


def _ts_mounts_and_middleware(text: str):
    """``X.use("/p", routerVar, mw, ...)`` -> ({var: prefix}, [middleware names]).

    A ``.use`` argument that is a literal string is a mount prefix; Router()
    variables are mounted sub-routers; any other named argument is a
    syntactically visible middleware for ``attrs.auth_hint``.
    """
    router_vars = set(_TS_ROUTER_VAR_RE.findall(text))
    mounts: dict[str, str] = {}
    middleware: list[str] = []
    for m in _TS_USE_RE.finditer(text):
        open_paren = text.index("(", m.start(), m.end())
        span = _brace_span(text, open_paren, "(", ")")
        if not span:
            continue
        args_text = text[span[0] + 1 : span[1]]
        prefix = ""
        sm = _TS_STRING_RE.search(args_text)
        if sm and sm.group(2).startswith("/"):
            prefix = sm.group(2)
        for arg in _ts_top_level_args(args_text):
            arg = arg.strip()
            if not arg or arg[0] in "'\"`":
                continue
            ident = re.match(r"[A-Za-z_$][\w$.]*", arg)
            if not ident:
                continue
            ident = ident.group(0)
            tail = ident.rsplit(".", 1)[-1]
            if ident in router_vars:
                if prefix:
                    mounts[ident] = prefix
            elif tail not in middleware:
                middleware.append(tail)
    return mounts, middleware


def _ts_top_level_args(args_text: str) -> list[str]:
    """Split a call's argument list on commas at paren/brace depth 0."""
    args, depth, start = [], 0, 0
    in_str = None
    for i, c in enumerate(args_text):
        if in_str:
            if c == in_str and args_text[i - 1] != "\\":
                in_str = None
            continue
        if c in "'\"`":
            in_str = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            args.append(args_text[start:i])
            start = i + 1
    tail = args_text[start:]
    if tail.strip():
        args.append(tail)
    return args


_TS_PLAIN_IDENT_RE = re.compile(r"^[A-Za-z_$][\w$.]*$")


def _ts_handler_name(inner_args: str) -> str:
    """Handler identifier among the call args: the last top-level argument
    when it is a plain reference; inline functions/arrows are unnamed."""
    args = _ts_top_level_args(inner_args)
    if len(args) < 2:
        return ""
    last = args[-1].strip()
    if _TS_PLAIN_IDENT_RE.match(last):
        return last.rsplit(".", 1)[-1]
    return ""


def _extract_ts_file(rel: str, text: str, result: AdapterResult):
    mounts, middleware = _ts_mounts_and_middleware(text)
    attrs = {"auth_hint": middleware} if middleware else {}
    seen_spans = set()

    def emit(method, raw, symbol, pos):
        _emit(
            result,
            rel=rel,
            method=method,
            raw_template=raw,
            symbol=symbol,
            line=_line_of(text, pos),
            prefix="",
            lang="ts",
            attrs=dict(attrs),
        )
        if symbol and symbol not in seen_spans:
            span = _func_span(text, symbol)
            if span:
                seen_spans.add(symbol)
                result.symbols.append(
                    SymbolSpan(
                        file=rel,
                        symbol=symbol,
                        start_line=span[0],
                        end_line=span[1],
                    )
                )

    # app|router.<verb>("/path", handler...) with optional .use mount prefix
    for m in _TS_VERB_RE.finditer(text):
        recv, verb, raw = m.group(1), m.group(2), m.group(4)
        if recv not in _TS_RECEIVERS and recv not in mounts:
            continue
        open_paren = text.index("(", m.start(), m.end())
        span = _brace_span(text, open_paren, "(", ")")
        args_text = text[span[0] + 1 : span[1]] if span else ""
        symbol = _ts_handler_name(args_text)
        template = join_prefix(mounts.get(recv, ""), raw)
        emit(None if verb == "all" else verb, template, symbol, m.start())

    # .route("/path").get(h).post(h) chains
    for m in _TS_ROUTE_CHAIN_RE.finditer(text):
        raw = m.group(2)
        stmt_end = text.find(";", m.end())
        tail_text = text[m.end() : stmt_end if stmt_end != -1 else len(text)]
        verbs = _TS_CHAIN_VERB_RE.findall(tail_text)
        if not verbs:
            continue
        for verb in verbs:
            emit(None if verb == "all" else verb, raw, "", m.start())

    # Fastify-style { method, url } / { method, path } object literals
    pos = 0
    while True:
        b = text.find("{", pos)
        if b == -1:
            break
        span = _brace_span(text, b, "{", "}")
        if not span:
            break
        body = text[span[0] + 1 : span[1]]
        method_m = re.search(
            r"\bmethod\s*:\s*(\[[^\]]*\]|[^\n,}]+)", body
        )
        url_m = re.search(
            r"\b(?:url|path)\s*:\s*(['\"`])(/[^'\"`]*)\1", body
        )
        if method_m and url_m:
            verbs = [
                v.lower()
                for v in re.findall(r"['\"`]([A-Za-z]+)['\"`]", method_m.group(1))
                if v.lower() in HTTP_VERBS
            ]
            if not verbs:
                # a {method, url} object with no HTTP verb is not a route
                pos = span[1]
                continue
            handler_m = re.search(
                r"\bhandler\s*:\s*([A-Za-z_$][\w$]*)", body
            )
            symbol = handler_m.group(1) if handler_m else ""
            for verb in verbs:
                emit(verb, url_m.group(2), symbol, b)
        pos = span[1]

    # Next.js file conventions
    route = next_route_from_path(rel)
    if route is not None:
        verbs = [
            next(g for g in m.groups() if g).lower()
            for m in _NEXT_EXPORT_VERB_RE.finditer(text)
        ]
        if not verbs:
            emit(None, route, "default", 0)
        else:
            for verb in verbs:
                symbol = verb.upper()
                emit(verb, route, symbol, 0)


def _extract_ts(root: Path, matcher: PathMatcher, result: AdapterResult):
    for f in sorted(root.rglob("*")):
        if f.suffix not in _TS_EXTS or not f.is_file():
            continue
        rel = f.relative_to(root).as_posix()
        if not matcher.matches(rel):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            result.warnings.append(f"{EXTRACTOR}: unreadable file {rel}: {exc}")
            continue
        _extract_ts_file(rel, text, result)


# ---- entry point ----


def extract(root) -> AdapterResult:
    """Discover all Python and TS/JS HTTP routes under ``root``.

    Returns an AdapterResult so the call site in ``extract.py`` picks up
    records, the symbol-table sidecar, and warnings in one shape (KTD7).
    """
    result = AdapterResult()
    root = Path(root)
    matcher = PathMatcher()
    _extract_python(root, matcher, result)
    _extract_ts(root, matcher, result)
    return result
