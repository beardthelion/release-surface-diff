//! HTTP route surface (R9): axum Router chains (route, route_service,
//! nest, merge), actix App/web::resource/web::scope plus attribute
//! routes, and warp path()/path! filters composed with and/or.

use std::collections::{HashMap, HashSet};

use serde_json::json;
use syn::spanned::Spanned;
use syn::visit::{self, Visit};
use syn::{Attribute, Expr, ExprMacro, File, ItemFn, Local, Pat};

use crate::model::{attrs, str_list, Extraction, Record};
use crate::util::{
    assign_parts, attr_exprs, attr_name, attr_single_lit, canonical_template, expr_path,
    lit_str, method_chain, path_string, path_tail, start_line, Scope,
};

const VERBS: &[&str] = &[
    "get", "post", "put", "delete", "patch", "head", "options", "trace",
];
const WARP_HANDLERS: &[&str] = &["map", "and_then", "then", "or_else"];
// Call tails treated as filter combinators when they are not warp-rooted.
const WARP_FILTER_TAILS: &[&str] = &[
    "path", "get", "post", "put", "delete", "patch", "head", "options", "any",
    "body", "header", "headers", "query", "cookie", "method", "host", "addr",
    "ws", "sse", "log", "cors", "trace", "ext", "multipart", "filters",
];
const ACTIX_CALLS: &[&str] = &[
    "service", "route", "wrap", "wrap_fn", "configure", "default_service", "app_data",
    "data", "name", "guard", "to",
];

const MAX_DEPTH: usize = 8;

pub fn extract(file: &File, path: &str, out: &mut Extraction) {
    let mut coll = BindingVisit::default();
    coll.visit_file(file);
    let mut v = RouteVisit {
        path,
        out,
        bindings: coll.bindings,
        consumed_inits: coll.consumed_inits,
        scope: Scope::default(),
        enclosing: Vec::new(),
    };
    v.visit_file(file);
}

// ---- pass A: let-bindings and nest/merge-consumed names ----

#[derive(Default)]
struct BindingVisit<'ast> {
    /// `let name = expr` initializers, file-wide.
    bindings: HashMap<String, Vec<&'ast Expr>>,
    /// Init exprs of bindings consumed as nest/merge router arguments:
    /// their routes are emitted through the mount site with the joined
    /// prefix, so standalone emission is suppressed.
    consumed_inits: HashSet<*const Expr>,
}

impl<'ast> BindingVisit<'ast> {
    fn record_arg_ident(&mut self, expr: &Expr) {
        if let Some(p) = expr_path(expr) {
            if p.segments.len() == 1 {
                let name = path_tail(p).unwrap();
                if let Some(inits) = self.bindings.get(&name) {
                    for e in inits {
                        self.consumed_inits.insert(*e as *const Expr);
                    }
                }
            }
        }
    }
}

impl<'ast> Visit<'ast> for BindingVisit<'ast> {
    fn visit_local(&mut self, i: &'ast Local) {
        if let (Pat::Ident(id), Some(init)) = (&i.pat, &i.init) {
            self.bindings
                .entry(id.ident.to_string())
                .or_default()
                .push(&init.expr);
        }
        visit::visit_local(self, i);
    }

    fn visit_expr_method_call(&mut self, i: &'ast syn::ExprMethodCall) {
        let m = i.method.to_string();
        match m.as_str() {
            "nest" | "nest_service" => {
                if let Some(arg) = i.args.get(1) {
                    self.record_arg_ident(arg);
                }
            }
            "merge" => {
                if let Some(arg) = i.args.first() {
                    self.record_arg_ident(arg);
                }
            }
            _ => {}
        }
        visit::visit_expr_method_call(self, i);
    }
}

// ---- shared pieces ----

fn call_target_name(expr: &Expr) -> Option<String> {
    match expr {
        Expr::Path(_) => expr_path(expr).and_then(path_tail),
        Expr::Call(c) => expr_path(&c.func).and_then(path_tail),
        Expr::MethodCall(mc) => Some(mc.method.to_string()),
        Expr::Reference(r) => call_target_name(&r.expr),
        _ => None,
    }
}

/// Middleware names visible in `.layer(...)`/`.route_layer(...)` args:
/// `middleware::from_fn(auth)` -> `auth`, `TraceLayer::new()` ->
/// `TraceLayer`, a bare `my_layer` path -> `my_layer`.
fn middleware_names(expr: &Expr) -> Vec<String> {
    match expr {
        Expr::Call(c) => {
            if let Some(p) = expr_path(&c.func) {
                let tail = path_tail(p).unwrap_or_default();
                if matches!(
                    tail.as_str(),
                    "from_fn" | "from_fn_with_state" | "map_request" | "map_response"
                ) {
                    if let Some(inner) = c.args.first() {
                        return middleware_names(inner);
                    }
                }
                vec![path_tail(p).unwrap_or_default()]
            } else {
                Vec::new()
            }
        }
        Expr::Path(_) => expr_path(expr).and_then(path_tail).into_iter().collect(),
        Expr::MethodCall(mc) => vec![mc.method.to_string()],
        _ => Vec::new(),
    }
}

struct RouteEntry {
    template: String,
    verb: String,
    handler: String,
    hints: Vec<String>,
    line: usize,
}

// ---- axum ----

fn is_router_new(expr: &Expr) -> bool {
    if let Expr::Call(c) = expr {
        if let Some(p) = expr_path(&c.func) {
            return p.segments.iter().any(|s| s.ident == "Router")
                && matches!(path_tail(p).as_deref(), Some("new") | Some("with_state"));
        }
    }
    false
}

/// Parse an axum method-router argument into (verb, handler-expr)
/// pairs plus middleware names from `.route_layer` calls.
fn method_router(
    expr: &Expr,
) -> (Vec<(String, Expr)>, Vec<String>) {
    match expr {
        Expr::Call(c) => {
            if let Some(p) = expr_path(&c.func) {
                let tail = path_tail(p).unwrap_or_default();
                if tail == "any" || tail == "fallback" {
                    if let Some(h) = c.args.first() {
                        return (vec![("ANY".into(), h.clone())], Vec::new());
                    }
                }
                if VERBS.contains(&tail.as_str()) {
                    if let Some(h) = c.args.first() {
                        return (
                            vec![(tail.to_uppercase(), h.clone())],
                            Vec::new(),
                        );
                    }
                }
            }
            (Vec::new(), Vec::new())
        }
        Expr::MethodCall(mc) => {
            let (mut pairs, mut hints) = method_router(&mc.receiver);
            let m = mc.method.to_string();
            if VERBS.contains(&m.as_str()) {
                if let Some(h) = mc.args.first() {
                    pairs.push((m.to_uppercase(), h.clone()));
                }
            } else if m == "route_layer" || m == "layer" {
                if let Some(arg) = mc.args.first() {
                    hints.extend(middleware_names(arg));
                }
            }
            (pairs, hints)
        }
        Expr::Path(_) => (vec![("ANY".into(), expr.clone())], Vec::new()),
        _ => (Vec::new(), Vec::new()),
    }
}

/// Route template argument: a string literal, a `concat!` of literals,
/// or a `let`-bound name resolving to either. Anything else is left
/// unemitted rather than fabricated.
fn template_str(
    expr: &Expr,
    bindings: &HashMap<String, Vec<&Expr>>,
    depth: usize,
) -> Option<String> {
    if depth > MAX_DEPTH {
        return None;
    }
    if let Some(s) = lit_str(expr) {
        return Some(s);
    }
    if let Expr::Macro(m) = expr {
        return crate::util::concat_lit(m);
    }
    if let Some(p) = expr_path(expr) {
        if p.segments.len() == 1 {
            if let Some(name) = path_tail(p) {
                if let Some(inits) = bindings.get(&name) {
                    if let Some(init) = inits.first() {
                        return template_str(init, bindings, depth + 1);
                    }
                }
            }
        }
    }
    None
}

struct AxumCtx<'a, 'ast> {
    bindings: &'a HashMap<String, Vec<&'ast Expr>>,
}

impl<'ast> AxumCtx<'_, 'ast> {
    /// Routes registered by one `expr`: either an inline
    /// `Router::new()...` chain or a binding resolving to one.
    fn resolve(&self, expr: &'ast Expr, depth: usize) -> Vec<RouteEntry> {
        if depth > MAX_DEPTH {
            return Vec::new();
        }
        let (root, calls) = method_chain(expr);
        if is_router_new(root) {
            return self.interpret(&calls, depth);
        }
        if let Some(p) = expr_path(root) {
            if p.segments.len() == 1 {
                if let Some(name) = path_tail(p) {
                    if let Some(inits) = self.bindings.get(&name) {
                        if let Some(init) = inits.first() {
                            let entries =
                                self.resolve_binding_chain(init, &calls, depth);
                            if !entries.is_empty() {
                                return entries;
                            }
                        }
                    }
                }
            }
        }
        Vec::new()
    }

    /// A binding used as a chain root (`r.route("/b", ...)` where
    /// `let r = Router::new().route("/a", ...)`): base routes plus this
    /// chain's calls.
    fn resolve_binding_chain(
        &self,
        init: &'ast Expr,
        extra_calls: &[&'ast syn::ExprMethodCall],
        depth: usize,
    ) -> Vec<RouteEntry> {
        let mut entries = self.resolve(init, depth + 1);
        entries.extend(self.interpret(extra_calls, depth + 1));
        entries
    }

    fn interpret(
        &self,
        calls: &[&'ast syn::ExprMethodCall],
        depth: usize,
    ) -> Vec<RouteEntry> {
        if depth > MAX_DEPTH {
            return Vec::new();
        }
        let mut entries: Vec<(usize, RouteEntry)> = Vec::new();
        let mut layers: Vec<(usize, Vec<String>)> = Vec::new();
        for (idx, mc) in calls.iter().enumerate() {
            let m = mc.method.to_string();
            let line = start_line(mc.method.span());
            match m.as_str() {
                "route" => {
                    let Some(tpl) = mc
                        .args
                        .first()
                        .and_then(|a| template_str(a, self.bindings, depth))
                    else {
                        continue;
                    };
                    let tpl = canonical_template(&tpl);
                    let (pairs, hints) = mc
                        .args
                        .get(1)
                        .map(method_router)
                        .unwrap_or_default();
                    for (verb, handler) in pairs {
                        entries.push((
                            idx,
                            RouteEntry {
                                template: tpl.clone(),
                                verb,
                                handler: call_target_name(&handler).unwrap_or_default(),
                                hints: hints.clone(),
                                line,
                            },
                        ));
                    }
                }
                "route_service" | "nest_service" => {
                    let Some(tpl) = mc
                        .args
                        .first()
                        .and_then(|a| template_str(a, self.bindings, depth))
                    else {
                        continue;
                    };
                    entries.push((
                        idx,
                        RouteEntry {
                            template: canonical_template(&tpl),
                            verb: "ANY".into(),
                            handler: mc
                                .args
                                .get(1)
                                .and_then(call_target_name)
                                .unwrap_or_default(),
                            hints: Vec::new(),
                            line,
                        },
                    ));
                }
                "nest" => {
                    let Some(tpl) = mc
                        .args
                        .first()
                        .and_then(|a| template_str(a, self.bindings, depth))
                    else {
                        continue;
                    };
                    let prefix = canonical_template(&tpl);
                    if let Some(arg) = mc.args.get(1) {
                        for mut e in self.resolve(arg, depth + 1) {
                            e.template = join_templates(&prefix, &e.template);
                            entries.push((idx, e));
                        }
                    }
                }
                "merge" => {
                    if let Some(arg) = mc.args.first() {
                        for e in self.resolve(arg, depth + 1) {
                            entries.push((idx, e));
                        }
                    }
                }
                "layer" | "route_layer" => {
                    let names: Vec<String> = mc
                        .args
                        .iter()
                        .flat_map(middleware_names)
                        .collect();
                    layers.push((idx, names));
                }
                "fallback" | "fallback_service" => {
                    entries.push((
                        idx,
                        RouteEntry {
                            template: "/".into(),
                            verb: "ANY".into(),
                            handler: mc
                                .args
                                .first()
                                .and_then(call_target_name)
                                .unwrap_or_default(),
                            hints: Vec::new(),
                            line,
                        },
                    ));
                }
                _ => {}
            }
        }
        // axum layers apply to routes registered before them on the chain.
        entries
            .into_iter()
            .map(|(idx, mut e)| {
                for (lidx, names) in &layers {
                    if *lidx > idx {
                        for n in names {
                            if !e.hints.contains(n) {
                                e.hints.push(n.clone());
                            }
                        }
                    }
                }
                e
            })
            .collect()
    }
}

fn join_templates(prefix: &str, tpl: &str) -> String {
    if tpl == "/" || tpl.is_empty() {
        return prefix.to_string();
    }
    format!("{}{}", prefix.trim_end_matches('/'), tpl)
}

// ---- actix ----

fn is_actix_root(expr: &Expr) -> bool {
    if let Expr::Call(c) = expr {
        if let Some(p) = expr_path(&c.func) {
            let rendered = path_string(p);
            return matches!(
                path_tail(p).as_deref(),
                Some("new") | Some("scope") | Some("resource") | Some("App")
            ) && (rendered.contains("App")
                || rendered.contains("web::")
                || rendered == "scope"
                || rendered == "resource");
        }
    }
    false
}

/// `web::get().to(h)` / `web::route()` / `web::method(...)`: verb plus
/// handler for actix `route()` arguments.
fn actix_method_guard(expr: &Expr) -> (String, Option<String>) {
    let (root, calls) = method_chain(expr);
    let mut verb = "ANY".to_string();
    if let Expr::Call(c) = root {
        if let Some(p) = expr_path(&c.func) {
            if let Some(t) = path_tail(p) {
                if VERBS.contains(&t.as_str()) {
                    verb = t.to_uppercase();
                }
            }
        }
    }
    let mut handler = None;
    for mc in calls {
        if mc.method == "to" || mc.method == "call" {
            handler = mc.args.first().and_then(call_target_name);
        }
    }
    (verb, handler)
}

// ---- the visitor ----

struct RouteVisit<'a> {
    path: &'a str,
    out: &'a mut Extraction,
    bindings: HashMap<String, Vec<&'a Expr>>,
    consumed_inits: HashSet<*const Expr>,
    scope: Scope,
    enclosing: Vec<String>,
}

impl RouteVisit<'_> {
    fn enclosing_symbol(&self) -> String {
        self.enclosing.last().cloned().unwrap_or_default()
    }

    fn push_records(&mut self, entries: Vec<RouteEntry>, framework: &str) {
        for e in entries {
            let symbol = if e.handler.is_empty() {
                self.enclosing_symbol()
            } else {
                e.handler.clone()
            };
            let mut rec = Record::new(
                "route",
                format!("{} {}", e.verb, e.template),
                self.path,
                e.line,
                symbol,
            );
            rec.attrs = attrs(&[
                ("framework", json!(framework)),
                ("handler", json!(e.handler)),
            ]);
            if !e.hints.is_empty() {
                rec.attrs.insert("auth_hint".into(), str_list(&e.hints));
            }
            self.out.records.push(rec);
        }
    }

    /// `.service(<arg>)` handling for App/scope chains.
    fn actix_service(&mut self, arg: &Expr, prefix: &str, depth: usize) {
        if depth > MAX_DEPTH {
            return;
        }
        let (root, calls) = method_chain(arg);
        if let Expr::Call(c) = root {
            if let Some(p) = expr_path(&c.func) {
                match path_tail(p).as_deref() {
                    Some("scope") => {
                        if let Some(tpl) = c.args.first().and_then(lit_str) {
                            let scope_prefix =
                                join_templates(prefix, &canonical_template(&tpl));
                            for mc in &calls {
                                self.actix_call(mc, &scope_prefix, depth + 1);
                            }
                        }
                        return;
                    }
                    Some("resource") => {
                        if let Some(tpl) = c.args.first().and_then(lit_str) {
                            let tpl = join_templates(prefix, &canonical_template(&tpl));
                            let mut saw_route = false;
                            let mut entries = Vec::new();
                            for mc in &calls {
                                let m = mc.method.to_string();
                                let line = start_line(mc.method.span());
                                match m.as_str() {
                                    "route" => {
                                        saw_route = true;
                                        if let Some(mr) = mc.args.first() {
                                            let (verb, handler) =
                                                actix_method_guard(mr);
                                            entries.push(RouteEntry {
                                                template: tpl.clone(),
                                                verb,
                                                handler: handler.unwrap_or_default(),
                                                hints: Vec::new(),
                                                line,
                                            });
                                        }
                                    }
                                    "to" | "call" => {
                                        saw_route = true;
                                        entries.push(RouteEntry {
                                            template: tpl.clone(),
                                            verb: "ANY".into(),
                                            handler: mc
                                                .args
                                                .first()
                                                .and_then(call_target_name)
                                                .unwrap_or_default(),
                                            hints: Vec::new(),
                                            line,
                                        });
                                    }
                                    _ => {}
                                }
                            }
                            if !saw_route {
                                entries.push(RouteEntry {
                                    template: tpl,
                                    verb: "ANY".into(),
                                    handler: String::new(),
                                    hints: Vec::new(),
                                    line: start_line(c.span()),
                                });
                            }
                            self.push_records(entries, "actix");
                        }
                        return;
                    }
                    _ => {}
                }
            }
        }
        // Path args (`service(handler_fn)`) are covered by attribute
        // routes; unresolvable factories degrade silently.
    }

    /// One call on an App/scope/cfg chain at `prefix`.
    fn actix_call(&mut self, mc: &syn::ExprMethodCall, prefix: &str, depth: usize) {
        if depth > MAX_DEPTH {
            return;
        }
        let m = mc.method.to_string();
        match m.as_str() {
            "service" => {
                if let Some(arg) = mc.args.first() {
                    self.actix_service(arg, prefix, depth);
                }
            }
            "route" => {
                // App::route("/p", web::get().to(h)) / scope.route(...)
                if let Some(tpl) = mc.args.first().and_then(lit_str) {
                    let (verb, handler) = mc
                        .args
                        .get(1)
                        .map(actix_method_guard)
                        .unwrap_or(("ANY".into(), None));
                    self.push_records(
                        vec![RouteEntry {
                            template: join_templates(prefix, &canonical_template(&tpl)),
                            verb,
                            handler: handler.unwrap_or_default(),
                            hints: Vec::new(),
                            line: start_line(mc.method.span()),
                        }],
                        "actix",
                    );
                }
            }
            "configure" => {
                for arg in mc.args.iter() {
                    if let Expr::Closure(cl) = arg {
                        self.actix_expr(&cl.body, prefix, depth + 1);
                    }
                }
            }
            _ => {}
        }
    }

    /// Interpret `expr` as an actix registration chain.
    fn actix_expr(&mut self, expr: &Expr, prefix: &str, depth: usize) {
        if depth > MAX_DEPTH {
            return;
        }
        let (root, calls) = method_chain(expr);
        if is_actix_root(root) || cfg_chain(&calls) {
            for mc in &calls {
                self.actix_call(mc, prefix, depth);
            }
        } else if let Some(p) = expr_path(root) {
            // `scope_var.service(..)` through a binding: recover the
            // scope/resource prefix from the binding's initializer.
            if p.segments.len() == 1 {
                if let Some(name) = path_tail(p) {
                    if let Some(inits) = self.bindings.get(&name) {
                        if let Some(init) = inits.first() {
                            let (iroot, icalls) = method_chain(init);
                            let mut inner_prefix = prefix.to_string();
                            if let Expr::Call(c) = iroot {
                                if let Some(fp) = expr_path(&c.func) {
                                    if matches!(
                                        path_tail(fp).as_deref(),
                                        Some("scope") | Some("resource")
                                    ) {
                                        if let Some(tpl) =
                                            c.args.first().and_then(lit_str)
                                        {
                                            inner_prefix = join_templates(
                                                prefix,
                                                &canonical_template(&tpl),
                                            );
                                        }
                                    }
                                }
                            }
                            for mc in &icalls {
                                self.actix_call(mc, &inner_prefix, depth + 1);
                            }
                            for mc in &calls {
                                self.actix_call(mc, &inner_prefix, depth);
                            }
                        }
                    }
                }
            }
        }
    }

    /// Attribute routes: `#[get("/p")]`, `#[route("/p", method="X")]`.
    fn actix_attr_routes(&mut self, attrs_: &[Attribute], ident: &syn::Ident) {
        for attr in attrs_ {
            let name = attr_name(attr);
            if VERBS.contains(&name.as_str()) {
                if let Some(tpl) = attr_single_lit(attr) {
                    self.emit_attr_route(attr, ident, &name.to_uppercase(), &tpl);
                }
            } else if name == "route" {
                if let Some(exprs) = attr_exprs(attr) {
                    let tpl = exprs.first().and_then(lit_str);
                    let methods: Vec<String> = exprs
                        .iter()
                        .filter_map(assign_parts)
                        .filter(|(k, _)| k == "method")
                        .filter_map(|(_, v)| lit_str(v))
                        .collect();
                    if let Some(tpl) = tpl {
                        for m in methods {
                            self.emit_attr_route(attr, ident, &m.to_uppercase(), &tpl);
                        }
                    }
                }
            }
        }
    }

    fn emit_attr_route(
        &mut self,
        attr: &Attribute,
        ident: &syn::Ident,
        verb: &str,
        tpl: &str,
    ) {
        let symbol = self.scope.qualify(&ident.to_string());
        let mut rec = Record::new(
            "route",
            format!("{} {}", verb, canonical_template(tpl)),
            self.path,
            start_line(attr.span()),
            symbol,
        );
        rec.attrs = attrs(&[
            ("framework", json!("actix")),
            ("handler", json!(ident.to_string())),
        ]);
        self.out.records.push(rec);
    }
}

/// True when every call on the chain is an actix-registration verb —
/// catches `configure(|cfg| cfg.service(..))` bodies where the root is an
/// unresolvable `cfg` binding.
fn cfg_chain(calls: &[&syn::ExprMethodCall]) -> bool {
    !calls.is_empty()
        && calls
            .iter()
            .all(|mc| ACTIX_CALLS.contains(&mc.method.to_string().as_str()))
        && calls.iter().any(|mc| mc.method == "service" || mc.method == "route")
}

// ---- warp ----

struct WarpRoute {
    segs: Vec<String>,
    method: Option<String>,
    handler: Option<String>,
    hints: Vec<String>,
}

fn warp_path_macro(em: &ExprMacro) -> Option<Vec<String>> {
    if path_tail(&em.mac.path).as_deref() != Some("path") {
        return None;
    }
    // path!("a" / u64 / "b" / String): `/` is a binary operator to syn,
    // so split the token stream on top-level `/` puncts instead. Each
    // chunk that is a lone string literal is a literal segment; anything
    // else (a type, `..`) is a path parameter.
    let mut segs = Vec::new();
    let mut chunk: Vec<proc_macro2::TokenTree> = Vec::new();
    let mut flush = |chunk: &mut Vec<proc_macro2::TokenTree>| {
        if chunk.is_empty() {
            return;
        }
        let lit = if chunk.len() == 1 {
            if let proc_macro2::TokenTree::Literal(l) = &chunk[0] {
                let raw = l.to_string();
                raw.strip_prefix('"')
                    .and_then(|s| s.strip_suffix('"'))
                    .map(|s| s.to_string())
            } else {
                None
            }
        } else {
            None
        };
        segs.push(lit.unwrap_or_else(|| "{param}".to_string()));
        chunk.clear();
    };
    for tt in em.mac.tokens.clone().into_iter() {
        if let proc_macro2::TokenTree::Punct(p) = &tt {
            if p.as_char() == '/' && p.spacing() == proc_macro2::Spacing::Alone {
                flush(&mut chunk);
                continue;
            }
        }
        chunk.push(tt);
    }
    flush(&mut chunk);
    Some(segs)
}

/// Filter-tree evaluation: and() merges path segments and methods
/// pairwise, or() unions route sets, other combinators pass routes
/// through. Returns None when a leaf is not a recognizable filter at
/// all (so `foo.map(bar)` on arbitrary values is not a route).
fn warp_routes(
    expr: &Expr,
    bindings: &HashMap<String, Vec<&Expr>>,
    depth: usize,
) -> Option<Vec<WarpRoute>> {
    if depth > MAX_DEPTH {
        return None;
    }
    let identity = || Some(vec![WarpRoute {
        segs: Vec::new(),
        method: None,
        handler: None,
        hints: Vec::new(),
    }]);
    match expr {
        Expr::MethodCall(mc) => {
            let m = mc.method.to_string();
            match m.as_str() {
                "and" => {
                    let recv = warp_routes(&mc.receiver, bindings, depth + 1)?;
                    let arg = mc
                        .args
                        .first()
                        .map(|a| warp_routes(a, bindings, depth + 1))
                        .unwrap_or(None)
                        .unwrap_or_else(|| {
                            vec![WarpRoute {
                                segs: Vec::new(),
                                method: None,
                                handler: None,
                                hints: Vec::new(),
                            }]
                        });
                    let mut out = Vec::new();
                    for a in &recv {
                        for b in &arg {
                            let mut segs = a.segs.clone();
                            segs.extend(b.segs.clone());
                            let mut hints = a.hints.clone();
                            for h in &b.hints {
                                if !hints.contains(h) {
                                    hints.push(h.clone());
                                }
                            }
                            out.push(WarpRoute {
                                segs,
                                method: a.method.clone().or_else(|| b.method.clone()),
                                handler: a.handler.clone().or_else(|| b.handler.clone()),
                                hints,
                            });
                        }
                    }
                    Some(out)
                }
                "or" => {
                    let recv = warp_routes(&mc.receiver, bindings, depth + 1)?;
                    let mut out = recv;
                    if let Some(arg) = mc.args.first() {
                        if let Some(b) = warp_routes(arg, bindings, depth + 1) {
                            out.extend(b);
                        }
                    }
                    Some(out)
                }
                m if WARP_HANDLERS.contains(&m) => {
                    // routes keep their handler from the producing map
                    let mut rs = warp_routes(&mc.receiver, bindings, depth + 1)?;
                    let h = mc.args.first().and_then(call_target_name);
                    for r in &mut rs {
                        if r.handler.is_none() {
                            r.handler = h.clone();
                        }
                    }
                    Some(rs)
                }
                "with" => {
                    let mut rs = warp_routes(&mc.receiver, bindings, depth + 1)?;
                    let names: Vec<String> =
                        mc.args.iter().flat_map(call_target_name).collect();
                    for r in &mut rs {
                        for n in &names {
                            if !r.hints.contains(n) {
                                r.hints.push(n.clone());
                            }
                        }
                    }
                    Some(rs)
                }
                _ => warp_routes(&mc.receiver, bindings, depth + 1),
            }
        }
        Expr::Call(c) => {
            if let Some(p) = expr_path(&c.func) {
                let rendered = path_string(p);
                let tail = path_tail(p).unwrap_or_default();
                let warp_rooted = rendered.starts_with("warp::");
                if tail == "param" || tail == "tail" {
                    if warp_rooted || rendered == "param" || rendered == "tail" {
                        return Some(vec![WarpRoute {
                            segs: vec![format!("{{{}}}", tail)],
                            ..warp_blank()
                        }]);
                    }
                }
                if tail == "path" && (warp_rooted || rendered == "path") {
                    if let Some(s) = c.args.first().and_then(lit_str) {
                        return Some(vec![WarpRoute {
                            segs: vec![s],
                            ..warp_blank()
                        }]);
                    }
                    return identity();
                }
                if tail == "end" && warp_rooted {
                    return identity();
                }
                if VERBS.contains(&tail.as_str())
                    && (warp_rooted || !rendered.contains("::"))
                {
                    return Some(vec![WarpRoute {
                        method: Some(tail.to_uppercase()),
                        ..warp_blank()
                    }]);
                }
                if warp_rooted || WARP_FILTER_TAILS.contains(&tail.as_str()) {
                    return identity();
                }
            }
            None
        }
        Expr::Macro(em) => {
            if let Some(segs) = warp_path_macro(em) {
                return Some(vec![WarpRoute {
                    segs,
                    ..warp_blank()
                }]);
            }
            None
        }
        Expr::Path(_) => {
            if let Some(p) = expr_path(expr) {
                if p.segments.len() == 1 {
                    if let Some(name) = path_tail(p) {
                        if let Some(inits) = bindings.get(&name) {
                            if let Some(init) = inits.first() {
                                return warp_routes(init, bindings, depth + 1);
                            }
                        }
                    }
                }
            }
            None
        }
        Expr::Paren(p) => warp_routes(&p.expr, bindings, depth + 1),
        Expr::Reference(r) => warp_routes(&r.expr, bindings, depth + 1),
        _ => None,
    }
}

fn warp_blank() -> WarpRoute {
    WarpRoute {
        segs: Vec::new(),
        method: None,
        handler: None,
        hints: Vec::new(),
    }
}

impl<'ast> Visit<'ast> for RouteVisit<'ast> {
    fn visit_item_mod(&mut self, i: &'ast syn::ItemMod) {
        self.scope.mods.push(i.ident.to_string());
        visit::visit_item_mod(self, i);
        self.scope.mods.pop();
    }

    fn visit_item_fn(&mut self, i: &'ast ItemFn) {
        self.actix_attr_routes(&i.attrs, &i.sig.ident);
        self.enclosing
            .push(self.scope.qualify(&i.sig.ident.to_string()));
        visit::visit_item_fn(self, i);
        self.enclosing.pop();
    }

    fn visit_impl_item_fn(&mut self, i: &'ast syn::ImplItemFn) {
        self.actix_attr_routes(&i.attrs, &i.sig.ident);
        self.enclosing
            .push(self.scope.qualify(&i.sig.ident.to_string()));
        visit::visit_impl_item_fn(self, i);
        self.enclosing.pop();
    }

    fn visit_item_impl(&mut self, i: &'ast syn::ItemImpl) {
        if let Some(ty) = crate::symbols::type_name(&i.self_ty) {
            self.scope.imp.push(ty);
            visit::visit_item_impl(self, i);
            self.scope.imp.pop();
        } else {
            visit::visit_item_impl(self, i);
        }
    }

    fn visit_expr(&mut self, e: &'ast Expr) {
        if let Expr::MethodCall(mc) = e {
            let m = mc.method.to_string();
            // warp handler sites
            if WARP_HANDLERS.contains(&m.as_str()) {
                if let Some(routes) =
                    warp_routes(&mc.receiver, &self.bindings, 0)
                {
                    let line = start_line(mc.method.span());
                    let handler = mc.args.first().and_then(call_target_name);
                    let mut entries = Vec::new();
                    for r in routes {
                        let tpl = if r.segs.is_empty() {
                            "/".to_string()
                        } else {
                            format!("/{}", r.segs.join("/"))
                        };
                        entries.push(RouteEntry {
                            template: canonical_template(&tpl),
                            verb: r.method.clone().unwrap_or_else(|| "ANY".into()),
                            handler: r
                                .handler
                                .clone()
                                .or_else(|| handler.clone())
                                .unwrap_or_default(),
                            hints: r.hints,
                            line,
                        });
                    }
                    self.push_records(entries, "warp");
                    return;
                }
            }
            let (root, calls) = method_chain(e);
            if is_router_new(root) {
                // `let inner = Router::new()...` consumed by nest/merge:
                // its routes are emitted through the mount site.
                if self.consumed_inits.contains(&(e as *const Expr)) {
                    return;
                }
                let ctx = AxumCtx {
                    bindings: &self.bindings,
                };
                let entries = ctx.interpret(&calls, 0);
                self.push_records(entries, "axum");
                return;
            }
            if let Some(p) = expr_path(root) {
                if p.segments.len() == 1 && !calls.is_empty() {
                    // `r.route(..)` on a bound router or `cfg.service(..)`
                    if let Some(name) = path_tail(p) {
                        if self.bindings.contains_key(&name) {
                            if let Some(inits) = self.bindings.get(&name) {
                                if let Some(init) = inits.first() {
                                    if is_router_new_chain(
                                        init,
                                        &self.bindings,
                                        &mut HashSet::new(),
                                    ) {
                                        let ctx = AxumCtx {
                                            bindings: &self.bindings,
                                        };
                                        let entries = ctx
                                            .resolve_binding_chain(init, &calls, 0);
                                        self.push_records(entries, "axum");
                                        return;
                                    }
                                }
                            }
                        }
                    }
                    if cfg_chain(&calls) {
                        self.actix_expr(e, "", 0);
                        return;
                    }
                }
            }
            if is_actix_root(root) || (calls.len() > 0 && cfg_chain(&calls)) {
                self.actix_expr(e, "", 0);
                return;
            }
        }
        visit::visit_expr(self, e);
    }
}

/// Is `expr` (or the binding it names) a `Router::new()` chain?
fn is_router_new_chain(
    expr: &Expr,
    bindings: &HashMap<String, Vec<&Expr>>,
    seen: &mut HashSet<String>,
) -> bool {
    let (root, _calls) = method_chain(expr);
    if is_router_new(root) {
        return true;
    }
    if let Some(p) = expr_path(root) {
        if p.segments.len() == 1 {
            if let Some(name) = path_tail(p) {
                // `let cat = cat.unwrap()` self-referential rebinds would
                // recurse forever; stop at the first repeated name.
                if !seen.insert(name.clone()) {
                    return false;
                }
                if let Some(inits) = bindings.get(&name) {
                    if let Some(init) = inits.first() {
                        return is_router_new_chain(init, bindings, seen);
                    }
                }
            }
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use crate::testutil::{ids, run};

    #[test]
    fn axum_basic_and_nest_join() {
        let out = run(
            r#"
use axum::{routing::{get, post}, Router};
pub fn app() -> Router {
    let inner = Router::new().route("/users/:id", get(u)).route("/users", post(c));
    Router::new()
        .route("/health", get(h))
        .route("/items/{iid}", get(i))
        .nest("/api", inner)
}
fn u() {} fn c() {} fn h() {} fn i() {}
"#,
        );
        let routes = ids(&out, "route");
        assert_eq!(
            routes,
            vec![
                "GET /api/users/{id}".to_string(),
                "GET /health".to_string(),
                "GET /items/{iid}".to_string(),
                "POST /api/users".to_string(),
            ]
        );
        // nested binding consumed: no standalone /users routes
        assert!(!routes.contains(&"GET /users/{id}".to_string()));
    }

    #[test]
    fn axum_route_service_merge_wildcard() {
        let out = run(
            r#"
use axum::{routing::get, Router};
pub fn app() -> Router {
    let extra = Router::new().route("/status", get(s));
    Router::new()
        .route_service("/static", svc())
        .route("/orders/{*rest}", get(o))
        .merge(extra)
}
fn s() {} fn o() {} fn svc() {}
"#,
        );
        let routes = ids(&out, "route");
        assert!(routes.contains(&"ANY /static".to_string()));
        assert!(routes.contains(&"GET /orders/{rest}".to_string()));
        assert!(routes.contains(&"GET /status".to_string()));
    }

    #[test]
    fn axum_multi_verb_and_layer_hint() {
        let out = run(
            r#"
use axum::{middleware, routing::{get, post}, Router};
pub fn app() -> Router {
    Router::new()
        .route("/a", get(ha).post(hb))
        .route("/b", get(hc).route_layer(middleware::from_fn(auth)))
}
fn ha() {} fn hb() {} fn hc() {} fn auth() {}
"#,
        );
        let routes = ids(&out, "route");
        assert!(routes.contains(&"GET /a".to_string()));
        assert!(routes.contains(&"POST /a".to_string()));
        assert!(routes.contains(&"GET /b".to_string()));
        let b = out
            .records
            .iter()
            .find(|r| r.id == "GET /b")
            .expect("GET /b");
        assert_eq!(
            b.attrs.get("auth_hint").unwrap().as_array().unwrap()[0],
            "auth"
        );
    }

    #[test]
    fn actix_attr_resource_scope() {
        let out = run(
            r#"
use actix_web::{get, web, App, HttpResponse};
#[get("/users/{id}")]
async fn u() -> HttpResponse { HttpResponse::Ok().finish() }
#[route("/m", method = "GET", method = "HEAD")]
async fn m() -> HttpResponse { HttpResponse::Ok().finish() }
async fn l() -> HttpResponse { HttpResponse::Ok().finish() }
pub fn app() {
    App::new()
        .service(web::resource("/items").route(web::get().to(l)))
        .service(web::scope("/api").service(web::resource("/v").route(web::put().to(l))));
}
"#,
        );
        let routes = ids(&out, "route");
        for want in [
            "GET /users/{id}",
            "GET /m",
            "HEAD /m",
            "GET /items",
            "PUT /api/v",
        ] {
            assert!(routes.contains(&want.to_string()), "missing {}", want);
        }
    }

    #[test]
    fn warp_filters_and_or_path_macro() {
        let out = run(
            r#"
use warp::Filter;
async fn h() -> Result<impl warp::Reply, warp::Rejection> { Ok(warp::reply::reply()) }
async fn c() -> Result<impl warp::Reply, warp::Rejection> { Ok(warp::reply::reply()) }
pub fn routes() -> impl Filter {
    let a = warp::path("users").and(warp::path::param::<u64>()).and(warp::get()).map(h);
    let b = warp::path!("v1" / "things" / u64).and(warp::post()).and_then(c);
    a.or(b)
}
"#,
        );
        let routes = ids(&out, "route");
        assert!(routes.contains(&"GET /users/{param}".to_string()));
        assert!(routes.contains(&"POST /v1/things/{param}".to_string()));
    }

    #[test]
    fn non_route_maps_do_not_emit() {
        let out = run(
            r#"
fn f(v: Vec<i32>) -> Vec<i32> {
    v.iter().map(|x| x + 1).collect()
}
fn g(o: Option<i32>) -> Option<i32> {
    o.map(|x| x + 1).or_else(|| Some(0))
}
"#,
        );
        assert!(ids(&out, "route").is_empty());
    }
}

#[cfg(test)]
mod template_tests {
    use crate::testutil::{ids, run};

    #[test]
    fn bound_and_concat_templates_resolve() {
        let out = run(
            r#"
use axum::{routing::get, Router};
pub fn app() -> Router {
    let p = "/bound";
    Router::new()
        .route(p, get(h))
        .route(concat!("/cat", "-path"), get(h))
}
fn h() {}
"#,
        );
        let routes = ids(&out, "route");
        assert!(routes.contains(&"GET /bound".to_string()));
        assert!(routes.contains(&"GET /cat-path".to_string()));
    }
}
