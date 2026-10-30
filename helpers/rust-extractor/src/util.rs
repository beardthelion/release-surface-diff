//! Shared syntactic helpers for the extractor modules.

use proc_macro2::Span;
use syn::punctuated::Punctuated;
use syn::{Attribute, Expr, ExprAssign, ExprLit, ExprMacro, ExprPath, Lit, Meta, Path, Token};

/// 1-based start line of a span (proc-macro2 span-locations feature).
pub fn start_line(span: Span) -> usize {
    span.start().line
}

/// 1-based end line of a span.
pub fn end_line(span: Span) -> usize {
    span.end().line
}

/// String value when `expr` is a string literal.
pub fn lit_str(expr: &Expr) -> Option<String> {
    if let Expr::Lit(ExprLit {
        lit: Lit::Str(s), ..
    }) = expr
    {
        Some(s.value())
    } else {
        None
    }
}

/// Unwrap `Expr::Path`.
pub fn expr_path(expr: &Expr) -> Option<&Path> {
    if let Expr::Path(ExprPath { path, .. }) = expr {
        Some(path)
    } else {
        None
    }
}

/// `a::b::c` rendered without spaces.
pub fn path_string(path: &Path) -> String {
    path.segments
        .iter()
        .map(|s| s.ident.to_string())
        .collect::<Vec<_>>()
        .join("::")
}

/// Last path segment ident (e.g. `x` for `a::b::x`, `b` for `a::b::<T>`).
pub fn path_tail(path: &Path) -> Option<String> {
    path.segments.last().map(|s| s.ident.to_string())
}

/// Token-render an expression compactly (`Self::PROTO`, not
/// `Self :: PROTO`). Used only for non-literal protocol ids, where the
/// expression itself is the stable identity.
pub fn expr_render(expr: &Expr) -> String {
    let mut s = quote::quote!(#expr).to_string();
    for (pat, tight) in [
        (" :: ", "::"), (" . ", "."), (" (", "("), (" )", ")"), (" ,", ","),
        ("( ", "("), ("< ", "<"), (" >", ">"), (" ;", ";"), ("[ ", "["),
        (" ]", "]"),
    ] {
        s = s.replace(pat, tight);
    }
    s
}

/// Attribute's last path segment (`serde` for `#[serde(..)]`, `method`
/// for `#[jsonrpsee::core::method(..)]`).
pub fn attr_name(attr: &Attribute) -> String {
    attr.path()
        .segments
        .last()
        .map(|s| s.ident.to_string())
        .unwrap_or_default()
}

/// Parse `#[attr(a, b = "c", d(e = "f"))]` arguments as comma-separated
/// expressions. `b = "c"` parses as Expr::Assign; bare `a` as Expr::Path;
/// nested `d(..)` as Expr::Call. Returns None for non-list attrs or parse
/// failure.
pub fn attr_exprs(attr: &Attribute) -> Option<Vec<Expr>> {
    if let Meta::List(list) = &attr.meta {
        list.parse_args_with(Punctuated::<Expr, Token![,]>::parse_terminated)
            .ok()
            .map(|p| p.into_iter().collect())
    } else {
        None
    }
}

/// The single string-literal argument of `#[verb("/path")]` attrs.
pub fn attr_single_lit(attr: &Attribute) -> Option<String> {
    let exprs = attr_exprs(attr)?;
    exprs.first().and_then(lit_str)
}

/// Assign target name and value from an Expr::Assign inside attr args
/// (`method = "GET"` -> ("method", "\"GET\"" as lit)).
pub fn assign_parts(expr: &Expr) -> Option<(String, &Expr)> {
    if let Expr::Assign(ExprAssign { left, right, .. }) = expr {
        if let Some(p) = expr_path(left) {
            return Some((path_tail(p)?, right));
        }
    }
    None
}

/// Last segment of the path a `#[derive(..)]` lists, e.g. `Deserialize`.
pub fn derive_names(attr: &Attribute) -> Vec<String> {
    if attr_name(attr) != "derive" {
        return Vec::new();
    }
    let Ok(names) =
        attr.parse_args_with(Punctuated::<Path, Token![,]>::parse_terminated)
    else {
        return Vec::new();
    };
    names.iter().filter_map(path_tail).collect()
}

/// Any derive in `attrs` matches `wanted`?
pub fn derives(attrs: &[Attribute], wanted: &[&str]) -> bool {
    attrs.iter().any(|a| {
        derive_names(a)
            .iter()
            .any(|d| wanted.contains(&d.as_str()))
    })
}

/// Canonical route template (KTD5/KTD12 shared `{name}` form):
/// `:seg`, `*seg`, `{*seg}`, `{seg:regex}` all normalize to `{seg}`.
/// Ensures a leading slash.
pub fn canonical_template(raw: &str) -> String {
    let mut out = String::new();
    let mut chars = raw.chars().peekable();
    let mut at_seg_start = true; // start of a path segment
    while let Some(c) = chars.next() {
        if at_seg_start && c == ':' {
            let name: String = chars.by_ref().take_while(|&x| x != '/').collect();
            out.push_str(&format!("{{{}}}", name));
            at_seg_start = false;
            continue;
        }
        if at_seg_start && c == '*' {
            let name: String = chars.by_ref().take_while(|&x| x != '/').collect();
            out.push_str(&format!("{{{}}}", name));
            at_seg_start = false;
            continue;
        }
        if c == '{' {
            let inner: String = chars.by_ref().take_while(|&x| x != '}').collect();
            let name = inner.trim_start_matches('*');
            let name = name.split(':').next().unwrap_or(name);
            out.push_str(&format!("{{{}}}", name));
            at_seg_start = false;
            continue;
        }
        at_seg_start = c == '/';
        out.push(c);
    }
    if !out.starts_with('/') {
        out.insert(0, '/');
    }
    out
}

/// snake_case/CamelCase -> kebab-case for clap's derived flag and
/// subcommand names (`diff_report`/`DiffReport` -> `diff-report`,
/// `HTTPPort` -> `http-port`).
pub fn kebab(name: &str) -> String {
    let mut out = String::new();
    let chars: Vec<char> = name.chars().collect();
    for (i, c) in chars.iter().enumerate() {
        if *c == '_' {
            out.push('-');
            continue;
        }
        if c.is_uppercase() {
            let prev_lower = i > 0 && (chars[i - 1].is_lowercase() || chars[i - 1].is_ascii_digit());
            let acronym_end = i > 0
                && chars[i - 1].is_uppercase()
                && i + 1 < chars.len()
                && chars[i + 1].is_lowercase();
            if i > 0 && !out.ends_with('-') && (prev_lower || acronym_end) {
                out.push('-');
            }
            out.push(c.to_ascii_lowercase());
            continue;
        }
        out.push(*c);
    }
    out
}

/// Evaluate `concat!("a", "b", ..)` when every argument is a string
/// literal.
pub fn concat_lit(em: &ExprMacro) -> Option<String> {
    if path_tail(&em.mac.path).as_deref() != Some("concat") {
        return None;
    }
    let args = em
        .mac
        .parse_body_with(Punctuated::<Lit, Token![,]>::parse_terminated)
        .ok()?;
    let mut out = String::new();
    for lit in args {
        if let Lit::Str(s) = lit {
            out.push_str(&s.value());
        } else {
            return None;
        }
    }
    Some(out)
}

/// Peel `recv.a(..).b(..)` into (root expr, calls in root->leaf order).
pub fn method_chain(expr: &Expr) -> (&Expr, Vec<&syn::ExprMethodCall>) {
    let mut calls = Vec::new();
    let mut cur = expr;
    while let Expr::MethodCall(mc) = cur {
        calls.push(mc);
        cur = &mc.receiver;
    }
    calls.reverse();
    (cur, calls)
}

/// Qualified-name scope tracker shared by visitors that need mod/impl
/// context.
#[derive(Debug, Default)]
pub struct Scope {
    pub mods: Vec<String>,
    pub imp: Vec<String>,
}

impl Scope {
    /// `a::b::name` for the current module/impl context.
    pub fn qualify(&self, name: &str) -> String {
        let mut parts: Vec<String> = self.mods.clone();
        if let Some(ty) = self.imp.last() {
            parts.push(ty.clone());
        }
        parts.push(name.to_string());
        parts.join("::")
    }

    /// `a::b::` prefix for the current module (no impl).
    pub fn mod_prefix(&self) -> String {
        if self.mods.is_empty() {
            String::new()
        } else {
            format!("{}::", self.mods.join("::"))
        }
    }
}
