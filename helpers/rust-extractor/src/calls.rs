//! Intra-module call edges (KTD8): (caller, callee) pairs where the
//! callee resolves to a name defined in the same file. Unresolvable
//! callees are dropped; ambiguous method names are dropped rather than
//! guessed.

use syn::visit::{self, Visit};
use syn::{File, ItemImpl, ItemTrait, Path};

use crate::model::{CallEdge, Extraction};
use crate::symbols::{type_name, DefinedIndex};
use crate::util::{expr_path, path_string, Scope};

pub fn extract(file: &File, _path: &str, defined: &DefinedIndex, out: &mut Extraction) {
    let mut v = CallVisit {
        defined,
        out,
        scope: Scope::default(),
        caller: None,
    };
    v.visit_file(file);
}

struct CallVisit<'a> {
    defined: &'a DefinedIndex,
    out: &'a mut Extraction,
    scope: Scope,
    caller: Option<String>,
}

impl CallVisit<'_> {
    fn edge(&mut self, callee: String) {
        if let Some(caller) = &self.caller {
            if caller != &callee {
                self.out.edges.push(CallEdge {
                    caller: caller.clone(),
                    callee,
                });
            }
        }
    }

    /// Rewrite `self::x`, `super::x`, `crate::x`, `Self::m` into a
    /// qualified-name candidate for the current scope.
    fn candidates(&self, path: &Path) -> Vec<String> {
        let rendered = path_string(path);
        let segs: Vec<String> = path
            .segments
            .iter()
            .map(|s| s.ident.to_string())
            .collect();
        let first = segs.first().map(String::as_str).unwrap_or("");
        match first {
            "crate" => vec![segs[1..].join("::")],
            "self" => {
                vec![format!("{}{}", self.scope.mod_prefix(), segs[1..].join("::"))]
            }
            "Self" => {
                let rest = segs[1..].join("::");
                match self.scope.imp.last() {
                    Some(ty) => vec![self.scope.qualify(&rest), format!("{}::{}", ty, rest)],
                    None => vec![rest],
                }
            }
            "super" => {
                let mut mods = self.scope.mods.clone();
                let mut rest = segs[1..].to_vec();
                // leading super:: chains climb one module each
                while rest.first().map(String::as_str) == Some("super") {
                    mods.pop();
                    rest.remove(0);
                }
                mods.pop();
                let prefix = if mods.is_empty() {
                    String::new()
                } else {
                    format!("{}::", mods.join("::"))
                };
                vec![format!("{}{}", prefix, rest.join("::"))]
            }
            _ => {
                let mut v = vec![format!("{}{}", self.scope.mod_prefix(), rendered)];
                v.push(rendered);
                v
            }
        }
    }

    fn resolve_call(&mut self, path: &Path) {
        for cand in self.candidates(path) {
            if let Some(q) = self.defined.resolve(&cand, &self.scope.mod_prefix()) {
                self.edge(q);
                return;
            }
        }
    }
}

impl<'ast> Visit<'ast> for CallVisit<'ast> {
    fn visit_item_mod(&mut self, i: &'ast syn::ItemMod) {
        self.scope.mods.push(i.ident.to_string());
        visit::visit_item_mod(self, i);
        self.scope.mods.pop();
    }

    fn visit_item_impl(&mut self, i: &'ast ItemImpl) {
        if let Some(ty) = type_name(&i.self_ty) {
            self.scope.imp.push(ty);
            visit::visit_item_impl(self, i);
            self.scope.imp.pop();
        } else {
            visit::visit_item_impl(self, i);
        }
    }

    fn visit_item_trait(&mut self, i: &'ast ItemTrait) {
        self.scope.imp.push(i.ident.to_string());
        visit::visit_item_trait(self, i);
        self.scope.imp.pop();
    }

    fn visit_item_fn(&mut self, i: &'ast syn::ItemFn) {
        let prev = self.caller.replace(self.scope.qualify(&i.sig.ident.to_string()));
        visit::visit_item_fn(self, i);
        self.caller = prev;
    }

    fn visit_impl_item_fn(&mut self, i: &'ast syn::ImplItemFn) {
        let prev = self.caller.replace(self.scope.qualify(&i.sig.ident.to_string()));
        visit::visit_impl_item_fn(self, i);
        self.caller = prev;
    }

    fn visit_trait_item_fn(&mut self, i: &'ast syn::TraitItemFn) {
        let prev = self.caller.replace(self.scope.qualify(&i.sig.ident.to_string()));
        visit::visit_trait_item_fn(self, i);
        self.caller = prev;
    }

    fn visit_expr_call(&mut self, i: &'ast syn::ExprCall) {
        if let Some(p) = expr_path(&i.func) {
            self.resolve_call(p);
        }
        visit::visit_expr_call(self, i);
    }

    fn visit_expr_method_call(&mut self, i: &'ast syn::ExprMethodCall) {
        // x.m(): resolve only when exactly one defined `*::m` exists.
        let name = i.method.to_string();
        if let Some(cands) = self.defined.by_tail.get(&name) {
            if cands.len() == 1 {
                self.edge(cands[0].clone());
            }
        }
        visit::visit_expr_method_call(self, i);
    }
}

#[cfg(test)]
mod tests {
    use crate::testutil::run;

    #[test]
    fn edges_resolve_same_module_only() {
        let out = run(
            r#"
mod m {
    pub fn a() { b(); }
    fn b() {}
    pub fn outer_call() { super::top(); }
}
fn top() {}
fn caller() {
    m::a();
    missing::gone();
    std::process::exit(0);
}
"#,
        );
        let pairs: Vec<(String, String)> = out
            .edges
            .iter()
            .map(|e| (e.caller.clone(), e.callee.clone()))
            .collect();
        assert!(pairs.contains(&("m::a".into(), "m::b".into())));
        assert!(pairs.contains(&("m::outer_call".into(), "top".into())));
        assert!(pairs.contains(&("caller".into(), "m::a".into())));
        assert_eq!(pairs.len(), 3);
    }
}
