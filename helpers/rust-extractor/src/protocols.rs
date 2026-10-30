//! libp2p protocol registrations (R9). The helper emits ONLY structured
//! registrations — `*Protocol::new(<non-literal>)` and `concat!(..)` of
//! literals. Bare string literals are owned by the orchestrator's
//! protocol_ids pass (R10), which scans every language uniformly, so a
//! literal here is deliberately skipped to keep each literal to exactly
//! one record.

use serde_json::json;
use syn::spanned::Spanned;
use syn::visit::{self, Visit};
use syn::{Expr, File};

use crate::model::{attrs, Extraction, Record};
use crate::symbols::type_name;
use crate::util::{
    concat_lit, expr_path, expr_render, lit_str, path_tail, start_line, Scope,
};

const PROTO_CTOR: &[&str] = &["new", "try_new", "new_with", "from_str"];

pub fn extract(file: &File, path: &str, out: &mut Extraction) {
    let mut v = ProtoVisit {
        path,
        out,
        scope: Scope::default(),
        enclosing: Vec::new(),
    };
    v.visit_file(file);
}

struct ProtoVisit<'a> {
    path: &'a str,
    out: &'a mut Extraction,
    scope: Scope,
    enclosing: Vec<String>,
}

impl ProtoVisit<'_> {
    fn enclosing(&self) -> String {
        self.enclosing.last().cloned().unwrap_or_default()
    }

    /// `XxxProtocol::new(..)` call shapes -> protocol name argument.
    fn registration<'e>(&self, c: &'e syn::ExprCall) -> Option<&'e Expr> {
        let p = expr_path(&c.func)?;
        let tail = path_tail(p)?;
        if !PROTO_CTOR.contains(&tail.as_str()) {
            return None;
        }
        let ty = p.segments.iter().rev().nth(1)?.ident.to_string();
        if !ty.ends_with("Protocol") {
            return None;
        }
        c.args.first()
    }
}

impl<'ast> Visit<'ast> for ProtoVisit<'ast> {
    fn visit_item_mod(&mut self, i: &'ast syn::ItemMod) {
        self.scope.mods.push(i.ident.to_string());
        visit::visit_item_mod(self, i);
        self.scope.mods.pop();
    }

    fn visit_item_impl(&mut self, i: &'ast syn::ItemImpl) {
        if let Some(ty) = type_name(&i.self_ty) {
            self.scope.imp.push(ty);
            visit::visit_item_impl(self, i);
            self.scope.imp.pop();
        } else {
            visit::visit_item_impl(self, i);
        }
    }

    fn visit_item_fn(&mut self, i: &'ast syn::ItemFn) {
        self.enclosing
            .push(self.scope.qualify(&i.sig.ident.to_string()));
        visit::visit_item_fn(self, i);
        self.enclosing.pop();
    }

    fn visit_impl_item_fn(&mut self, i: &'ast syn::ImplItemFn) {
        self.enclosing
            .push(self.scope.qualify(&i.sig.ident.to_string()));
        visit::visit_impl_item_fn(self, i);
        self.enclosing.pop();
    }

    fn visit_expr_call(&mut self, e: &'ast syn::ExprCall) {
        if let Some(arg) = self.registration(e) {
            if lit_str(arg).is_some() {
                // bare literal: protocol_ids.py owns it exactly once
                return;
            }
            let (id, resolved) = match arg {
                Expr::Macro(m) => match concat_lit(m) {
                    Some(s) => (s, true),
                    None => (expr_render(arg), false),
                },
                _ => (expr_render(arg), false),
            };
            let mut rec = Record::new(
                "protocol_id",
                id,
                self.path,
                start_line(e.span()),
                self.enclosing(),
            );
            rec.attrs = attrs(&[
                ("framework", json!("libp2p")),
                ("structured", json!(true)),
                ("resolved_literal", json!(resolved)),
            ]);
            self.out.records.push(rec);
        }
        visit::visit_expr_call(self, e);
    }
}

#[cfg(test)]
mod tests {
    use crate::testutil::{ids, run};

    #[test]
    fn literal_skipped_structured_emitted() {
        let out = run(
            r#"
const PROTO: &str = "/eth2/status/1";
pub fn f() {
    let _a = StreamProtocol::new("/eth2/beacon_chain/req/status/1/ssz_snappy");
    let _b = StreamProtocol::new(PROTO);
    let _c = StreamProtocol::new(concat!("/eth2/req/", "blocks/1"));
}
"#,
        );
        let pids = ids(&out, "protocol_id");
        // the bare literal is orchestrator-owned: exactly absent here
        assert!(!pids.contains(&"/eth2/beacon_chain/req/status/1/ssz_snappy".to_string()));
        assert!(pids.contains(&"PROTO".to_string()));
        assert!(pids.contains(&"/eth2/req/blocks/1".to_string()));
    }
}
