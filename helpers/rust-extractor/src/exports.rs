//! Export surface (R9): `pub` items reachable outside the crate. An item
//! is exported when its own visibility is `pub` and every ancestor module
//! is `pub`; `pub(crate)`/`pub(super)`/`pub(in)` items and anything under
//! a private or crate-visible module is not an entry point.

use std::collections::HashSet;

use serde_json::json;
use syn::spanned::Spanned;
use syn::visit::{self, Visit};
use syn::{File, Item, ItemImpl, ItemMod, ItemTrait, UseTree, Visibility};

use crate::model::{attrs, Extraction, Record};
use crate::symbols::type_name;
use crate::util::{start_line, Scope};

pub fn extract(file: &File, path: &str, out: &mut Extraction) {
    // Pass 1: which qualified names are themselves exported (needed so
    // `impl PrivType { pub fn m }` does not leak m).
    let mut coll = CollVisit {
        exported: HashSet::new(),
        scope: Scope::default(),
    };
    coll.visit_file(file);

    let mut v = ExpVisit {
        path,
        out,
        scope: Scope::default(),
        public_stack: vec![true],
        exported: coll.exported,
    };
    v.visit_file(file);
}

fn is_pub(v: &Visibility) -> bool {
    matches!(v, Visibility::Public(_))
}

/// Item kinds that are exportable entry points (fn/type/const/etc.).
fn item_name(item: &Item) -> Option<String> {
    match item {
        Item::Fn(i) => Some(i.sig.ident.to_string()),
        Item::Struct(i) => Some(i.ident.to_string()),
        Item::Enum(i) => Some(i.ident.to_string()),
        Item::Union(i) => Some(i.ident.to_string()),
        Item::Const(i) => Some(i.ident.to_string()),
        Item::Static(i) => Some(i.ident.to_string()),
        Item::Type(i) => Some(i.ident.to_string()),
        _ => None,
    }
}

fn item_vis(item: &Item) -> Option<&Visibility> {
    match item {
        Item::Fn(i) => Some(&i.vis),
        Item::Struct(i) => Some(&i.vis),
        Item::Enum(i) => Some(&i.vis),
        Item::Union(i) => Some(&i.vis),
        Item::Const(i) => Some(&i.vis),
        Item::Static(i) => Some(&i.vis),
        Item::Type(i) => Some(&i.vis),
        Item::Trait(i) => Some(&i.vis),
        Item::Mod(i) => Some(&i.vis),
        Item::Use(i) => Some(&i.vis),
        _ => None,
    }
}

/// `pub use` leaf names: `a::b` -> `b`, `a::b as c` -> `c`, `a::*` -> `a::*`.
fn use_leaves(tree: &UseTree, prefix: &str, out: &mut Vec<String>) {
    match tree {
        UseTree::Path(p) => {
            let pfx = if prefix.is_empty() {
                p.ident.to_string()
            } else {
                format!("{}::{}", prefix, p.ident)
            };
            use_leaves(&p.tree, &pfx, out);
        }
        UseTree::Name(n) => out.push(n.ident.to_string()),
        UseTree::Rename(r) => out.push(r.rename.to_string()),
        UseTree::Glob(_) => out.push(format!("{}::*", prefix)),
        UseTree::Group(g) => {
            for t in &g.items {
                use_leaves(t, prefix, out);
            }
        }
    }
}

// ---- pass 1: exported qualified names ----

struct CollVisit {
    exported: HashSet<String>,
    scope: Scope,
}

impl<'ast> Visit<'ast> for CollVisit {
    fn visit_item_mod(&mut self, i: &'ast ItemMod) {
        if !is_pub(&i.vis) {
            return; // not reachable outside
        }
        self.exported.insert(self.scope.qualify(&i.ident.to_string()));
        self.scope.mods.push(i.ident.to_string());
        visit::visit_item_mod(self, i);
        self.scope.mods.pop();
    }

    fn visit_item(&mut self, item: &'ast Item) {
        if let (Some(name), Some(vis)) = (item_name(item), item_vis(item)) {
            if is_pub(vis) {
                self.exported.insert(self.scope.qualify(&name));
            }
        }
        // traits contribute method names too
        if let Item::Trait(t) = item {
            if is_pub(&t.vis) {
                for it in &t.items {
                    if let syn::TraitItem::Fn(f) = it {
                        self.exported.insert(
                            self.scope
                                .qualify(&format!("{}::{}", t.ident, f.sig.ident)),
                        );
                    }
                }
            }
        }
        visit::visit_item(self, item);
    }
}

// ---- pass 2: emit ----

struct ExpVisit<'a> {
    path: &'a str,
    out: &'a mut Extraction,
    scope: Scope,
    public_stack: Vec<bool>,
    exported: HashSet<String>,
}

impl ExpVisit<'_> {
    fn publicly_reachable(&self) -> bool {
        self.public_stack.iter().all(|p| *p)
    }

    fn emit(&mut self, qualified: String, line: usize, role: &str) {
        let mut rec = Record::new("export", qualified.clone(), self.path, line, qualified);
        rec.attrs = attrs(&[("role", json!(role))]);
        self.out.records.push(rec);
    }
}

impl<'ast> Visit<'ast> for ExpVisit<'ast> {
    fn visit_item_mod(&mut self, i: &'ast ItemMod) {
        let pub_mod = is_pub(&i.vis);
        if pub_mod && self.publicly_reachable() {
            self.emit(
                self.scope.qualify(&i.ident.to_string()),
                start_line(i.ident.span()),
                "mod",
            );
        }
        self.scope.mods.push(i.ident.to_string());
        self.public_stack.push(pub_mod);
        visit::visit_item_mod(self, i);
        self.public_stack.pop();
        self.scope.mods.pop();
    }

    fn visit_item(&mut self, item: &'ast Item) {
        if !self.publicly_reachable() {
            visit::visit_item(self, item);
            return;
        }
        match item {
            Item::Use(u) => {
                if is_pub(&u.vis) {
                    let mut leaves = Vec::new();
                    use_leaves(&u.tree, "", &mut leaves);
                    for leaf in leaves {
                        self.emit(
                            self.scope.qualify(&leaf),
                            start_line(u.use_token.span()),
                            "use",
                        );
                    }
                }
            }
            Item::Trait(t) => {
                if is_pub(&t.vis) {
                    let tname = t.ident.to_string();
                    self.emit(
                        self.scope.qualify(&tname),
                        start_line(t.ident.span()),
                        "trait",
                    );
                    for it in &t.items {
                        if let syn::TraitItem::Fn(f) = it {
                            self.emit(
                                self.scope
                                    .qualify(&format!("{}::{}", tname, f.sig.ident)),
                                start_line(f.sig.ident.span()),
                                "trait_method",
                            );
                        }
                    }
                }
            }
            _ => {
                if let (Some(name), Some(vis)) = (item_name(item), item_vis(item)) {
                    if is_pub(vis) {
                        self.emit(
                            self.scope.qualify(&name),
                            start_line(item.span()),
                            "item",
                        );
                    }
                }
            }
        }
        visit::visit_item(self, item);
    }

    fn visit_item_impl(&mut self, i: &'ast ItemImpl) {
        if !self.publicly_reachable() {
            return;
        }
        if let Some(ty) = type_name(&i.self_ty) {
            let ty_q = self.scope.qualify(&ty);
            // inherent impl methods are exported only when the type is
            if self.exported.contains(&ty_q) {
                for it in &i.items {
                    if let syn::ImplItem::Fn(f) = it {
                        if is_pub(&f.vis) {
                            self.emit(
                                self.scope
                                    .qualify(&format!("{}::{}", ty, f.sig.ident)),
                                start_line(f.sig.ident.span()),
                                "method",
                            );
                        }
                    }
                }
            }
            // still descend for nested items (consts etc. handled above
            // is skipped since impl items are not visited by visit_item)
        }
    }

    fn visit_item_trait(&mut self, _i: &'ast ItemTrait) {
        // trait surface emitted in visit_item; no recursion needed for
        // exports (trait bodies cannot contain pub items that add to it)
    }
}

#[cfg(test)]
mod tests {
    use crate::testutil::{ids, run};

    #[test]
    fn pub_visibility_rules() {
        let out = run(
            r#"
pub mod api {
    pub fn serve() {}
    pub(crate) fn internal() {}
    mod hidden { pub fn secret() {} }
}
pub(crate) mod internals { pub fn helper() {} }
pub fn main_entry() {}
pub(crate) fn crate_only() {}
fn private_fn() {}
pub struct Public;
struct Private;
impl Private { pub fn leaked(&self) {} }
impl Public { pub fn method(&self) {} }
pub use api::serve as run_it;
"#,
        );
        let exports = ids(&out, "export");
        for want in [
            "api",
            "api::serve",
            "main_entry",
            "Public",
            "Public::method",
            "run_it",
        ] {
            assert!(exports.contains(&want.to_string()), "missing {}", want);
        }
        for banned in [
            "api::internal",
            "api::hidden::secret",
            "internals::helper",
            "crate_only",
            "private_fn",
            "Private",
            "Private::leaked",
        ] {
            assert!(
                !exports.contains(&banned.to_string()),
                "{} should not be exported",
                banned
            );
        }
    }
}
