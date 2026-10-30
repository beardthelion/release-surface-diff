//! serde config structs (R9): fields of structs deriving `Deserialize`
//! emit dotted `config_key` paths; `#[serde(rename)]` (including
//! `rename(deserialize = "..")`) is honored, `#[serde(flatten)]` inlines
//! the nested keys, and fields whose type is another Deserialize struct
//! in the same file recurse.

use std::collections::{HashMap, HashSet};

use serde_json::json;
use syn::visit::{self, Visit};
use syn::{Attribute, Expr, Fields, File, ItemStruct, Type};

use crate::model::{attrs, Extraction, Record};
use crate::util::{
    assign_parts, attr_exprs, attr_name, derives, expr_path, lit_str, path_tail,
    start_line, Scope,
};

pub fn extract(file: &File, path: &str, out: &mut Extraction) {
    // Pass 1: which structs deserialize and what their fields look like.
    let mut coll = CollVisit {
        structs: HashMap::new(),
    };
    coll.visit_file(file);

    let mut v = CfgVisit {
        path,
        out,
        scope: Scope::default(),
        structs: coll.structs,
    };
    v.visit_file(file);
}

#[derive(Debug, Clone)]
struct Field {
    /// Rust field name (for the record's symbol)
    name: String,
    /// config key segment (rename honored) or field name
    key: String,
    line: usize,
    /// `#[serde(flatten)]`
    flatten: bool,
    /// `#[serde(skip)]` / `skip_deserializing`
    skip: bool,
    /// innermost type tail (`Vec<P2pConfig>` -> `P2pConfig`)
    type_tail: Option<String>,
}

#[derive(Default)]
struct CollVisit {
    /// struct name -> its named fields
    structs: HashMap<String, Vec<Field>>,
}

/// serde rename for a field: `rename(deserialize = "x")` and
/// `rename = "x"` both honored, deserialize spelling preferred.
fn serde_rename(attrs: &[Attribute]) -> Option<String> {
    let mut plain: Option<String> = None;
    for a in attrs {
        if attr_name(a) != "serde" {
            continue;
        }
        if let Some(exprs) = attr_exprs(a) {
            for e in &exprs {
                match e {
                    // rename(deserialize = "x")
                    Expr::Call(c) => {
                        if expr_path(&c.func).and_then(path_tail).as_deref()
                            == Some("rename")
                        {
                            for (k, v) in c.args.iter().filter_map(assign_parts) {
                                if k == "deserialize" {
                                    if let Some(s) = lit_str(v) {
                                        return Some(s);
                                    }
                                }
                            }
                        }
                    }
                    _ => {
                        if let Some((k, v)) = assign_parts(e) {
                            if k == "rename" {
                                if let Some(s) = lit_str(v) {
                                    plain = Some(s);
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    plain
}

/// Bare `#[serde(flatten)]` / `#[serde(skip)]` style flags.
fn serde_flag(attrs: &[Attribute], wanted: &str) -> bool {
    for a in attrs {
        if attr_name(a) != "serde" {
            continue;
        }
        if let Some(exprs) = attr_exprs(a) {
            for e in &exprs {
                if expr_path(e).and_then(path_tail).as_deref() == Some(wanted) {
                    return true;
                }
            }
        }
    }
    false
}

/// Strip `Option`/`Vec`/`Box`/`HashMap`/`BTreeMap` wrappers to the inner
/// type's last segment, or the type's own last segment.
fn type_tail(ty: &Type) -> Option<String> {
    if let Type::Path(tp) = ty {
        let last = tp.path.segments.last()?;
        let name = last.ident.to_string();
        if matches!(
            name.as_str(),
            "Option" | "Vec" | "Box" | "HashMap" | "BTreeMap" | "Arc" | "Rc"
        ) {
            if let syn::PathArguments::AngleBracketed(args) = &last.arguments {
                if let Some(syn::GenericArgument::Type(inner)) = args.args.first() {
                    return type_tail(inner);
                }
            }
            return Some(name);
        }
        return Some(name);
    }
    if let Type::Reference(r) = ty {
        return type_tail(&r.elem);
    }
    None
}

impl<'ast> Visit<'ast> for CollVisit {
    fn visit_item_struct(&mut self, i: &'ast ItemStruct) {
        if derives(&i.attrs, &["Deserialize"]) {
            let mut fields = Vec::new();
            if let Fields::Named(named) = &i.fields {
                for f in &named.named {
                    let Some(ident) = &f.ident else { continue };
                    let fname = ident.to_string();
                    let rename = serde_rename(&f.attrs);
                    let skip = serde_flag(&f.attrs, "skip")
                        || serde_flag(&f.attrs, "skip_deserializing");
                    fields.push(Field {
                        name: fname.clone(),
                        key: rename.unwrap_or(fname),
                        line: start_line(ident.span()),
                        flatten: serde_flag(&f.attrs, "flatten"),
                        skip,
                        type_tail: type_tail(&f.ty),
                    });
                }
            }
            self.structs.insert(i.ident.to_string(), fields);
        }
        visit::visit_item_struct(self, i);
    }
}

struct CfgVisit<'a> {
    path: &'a str,
    out: &'a mut Extraction,
    scope: Scope,
    structs: HashMap<String, Vec<Field>>,
}

impl CfgVisit<'_> {
    /// Emit `st`'s fields under `prefix`, recursing into nested
    /// Deserialize structs. `visited` guards recursive types; it is
    /// scoped per path so two fields of the same type both expand.
    fn emit_struct(&mut self, st: &str, prefix: &str, visited: &mut HashSet<String>) {
        if visited.contains(st) {
            return;
        }
        visited.insert(st.to_string());
        let Some(fields) = self.structs.get(st).cloned() else {
            visited.remove(st);
            return;
        };
        for f in &fields {
            if f.skip {
                continue;
            }
            let inner = f
                .type_tail
                .as_deref()
                .filter(|t| self.structs.contains_key(*t));
            if f.flatten {
                // inline the nested keys at the current prefix
                if let Some(inner) = inner {
                    self.emit_struct(inner, prefix, visited);
                }
                continue;
            }
            let id = if prefix.is_empty() {
                f.key.clone()
            } else {
                format!("{}.{}", prefix, f.key)
            };
            let mut rec = Record::new(
                "config_key",
                id.clone(),
                self.path,
                f.line,
                self.scope.qualify(&format!("{}::{}", st, f.name)),
            );
            rec.attrs = attrs(&[("serde", json!(true))]);
            self.out.records.push(rec);
            if let Some(inner) = inner {
                self.emit_struct(inner, &id, visited);
            }
        }
        visited.remove(st);
    }
}

impl<'ast> Visit<'ast> for CfgVisit<'ast> {
    fn visit_item_mod(&mut self, i: &'ast syn::ItemMod) {
        self.scope.mods.push(i.ident.to_string());
        visit::visit_item_mod(self, i);
        self.scope.mods.pop();
    }

    fn visit_item_struct(&mut self, i: &'ast ItemStruct) {
        if derives(&i.attrs, &["Deserialize"]) {
            let mut visited = HashSet::new();
            self.emit_struct(&i.ident.to_string(), "", &mut visited);
        }
        visit::visit_item_struct(self, i);
    }
}

#[cfg(test)]
mod tests {
    use crate::testutil::{ids, run};

    #[test]
    fn serde_rename_nested_flatten() {
        let out = run(
            r#"
use serde::Deserialize;
#[derive(Deserialize)]
pub struct NodeConfig {
    pub datadir: String,
    #[serde(rename = "http-port")]
    pub http_port: u16,
    #[serde(rename = "p2p")]
    pub network: P2pConfig,
    #[serde(flatten)]
    pub common: CommonConfig,
}
#[derive(Deserialize)]
pub struct P2pConfig {
    pub port: u16,
    #[serde(rename(deserialize = "boot-nodes"))]
    pub boot: Vec<String>,
    #[serde(skip)]
    pub secret: String,
}
#[derive(Deserialize)]
pub struct CommonConfig {
    #[serde(rename = "log-level")]
    pub log_level: String,
}
pub struct NotSerde { pub x: u8 }
"#,
        );
        let keys = ids(&out, "config_key");
        for want in [
            "datadir",
            "http-port",
            "p2p",
            "p2p.port",
            "p2p.boot-nodes",
            "log-level",
        ] {
            assert!(keys.contains(&want.to_string()), "missing {}", want);
        }
        assert!(!keys.contains(&"p2p.secret".to_string()));
        assert!(!keys.iter().any(|k| k.contains("NotSerde") || k == "x"));
    }
}
