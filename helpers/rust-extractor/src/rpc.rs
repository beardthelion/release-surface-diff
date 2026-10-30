//! RPC surface (R9): tonic `add_service(XServer::new(..))`, jsonrpsee
//! `#[rpc]` traits (`#[method]`/`#[subscription]`), and jsonrpsee manual
//! `register_method`/`register_async_method`/`register_subscription`
//! calls.

use serde_json::json;
use syn::visit::{self, Visit};
use syn::{Expr, File};

use crate::model::{attrs, Extraction, Record};
use crate::symbols::type_name;
use crate::util::{
    assign_parts, attr_exprs, attr_name, expr_path, expr_render, lit_str, path_string,
    path_tail, start_line, Scope,
};

pub fn extract(file: &File, path: &str, out: &mut Extraction) {
    let mut v = RpcVisit {
        path,
        out,
        scope: Scope::default(),
        enclosing: Vec::new(),
        rpc_ns: Vec::new(),
    };
    v.visit_file(file);
}

struct RpcVisit<'a> {
    path: &'a str,
    out: &'a mut Extraction,
    scope: Scope,
    enclosing: Vec<String>,
    /// Namespace stack parallel to trait visits: Some when the enclosing
    /// trait carries `#[rpc(...)]` (its `namespace = "x"` value, if any).
    rpc_ns: Vec<Option<String>>,
}

impl RpcVisit<'_> {
    fn enclosing(&self) -> String {
        self.enclosing.last().cloned().unwrap_or_default()
    }

    fn emit(&mut self, id: String, symbol: String, line: usize, framework: &str) {
        if id.is_empty() {
            return;
        }
        let mut rec = Record::new("rpc", id, self.path, line, symbol);
        rec.attrs = attrs(&[("framework", json!(framework))]);
        self.out.records.push(rec);
    }
}

/// `namespace = "eth"` (or `name = "eth"`) inside `#[rpc(...)]`.
fn rpc_namespace(attrs: &[syn::Attribute]) -> Option<Option<String>> {
    for a in attrs {
        if attr_name(a) == "rpc" {
            if let Some(exprs) = attr_exprs(a) {
                for (k, v) in exprs.iter().filter_map(assign_parts) {
                    if k == "namespace" || k == "name" {
                        if let Some(ns) = lit_str(v) {
                            return Some(Some(ns));
                        }
                    }
                }
            }
            return Some(None);
        }
    }
    None
}

/// `name = "x"` inside `#[method(...)]` / `#[subscription(...)]`.
fn method_name_attr(attrs: &[syn::Attribute]) -> Option<String> {
    for a in attrs {
        if matches!(attr_name(a).as_str(), "method" | "subscription") {
            if let Some(exprs) = attr_exprs(a) {
                for (k, v) in exprs.iter().filter_map(assign_parts) {
                    if k == "name" {
                        if let Some(n) = lit_str(v) {
                            return Some(n);
                        }
                    }
                }
            }
        }
    }
    None
}

/// The `Xxx` in `XxxServer::new(..)` / `XxxServer::with_interceptor(..)`
/// when the call creates a tonic server type.
fn tonic_service(expr: &Expr) -> Option<String> {
    if let Expr::Call(c) = expr {
        if let Some(p) = expr_path(&c.func) {
            let tail = path_tail(p)?;
            if matches!(
                tail.as_str(),
                "new" | "with_interceptor" | "from_arc" | "from_shared"
            ) {
                let ty = p
                    .segments
                    .iter()
                    .rev()
                    .nth(1)
                    .map(|s| s.ident.to_string())?;
                if let Some(svc) = ty.strip_suffix("Server") {
                    if !svc.is_empty() {
                        return Some(svc.to_string());
                    }
                }
            }
        }
    }
    None
}

impl<'ast> Visit<'ast> for RpcVisit<'ast> {
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

    fn visit_item_trait(&mut self, i: &'ast syn::ItemTrait) {
        self.rpc_ns.push(rpc_namespace(&i.attrs).flatten());
        self.scope.imp.push(i.ident.to_string());
        visit::visit_item_trait(self, i);
        self.scope.imp.pop();
        self.rpc_ns.pop();
    }

    fn visit_trait_item_fn(&mut self, i: &'ast syn::TraitItemFn) {
        if let Some(ns) = self.rpc_ns.last() {
            // Inside a #[rpc] trait every method is an endpoint: explicit
            // `name =` wins, else namespace + "_" + fn name.
            let fn_name = i.sig.ident.to_string();
            let id = method_name_attr(&i.attrs).or_else(|| {
                ns.as_ref().map(|ns| format!("{}_{}", ns, fn_name))
            });
            if let Some(id) = id {
                self.emit(
                    id,
                    self.scope.qualify(&fn_name),
                    start_line(i.sig.ident.span()),
                    "jsonrpsee",
                );
            }
        }
        visit::visit_trait_item_fn(self, i);
    }

    fn visit_expr_method_call(&mut self, i: &'ast syn::ExprMethodCall) {
        let m = i.method.to_string();
        match m.as_str() {
            "add_service" => {
                if let Some(arg) = i.args.first() {
                    if let Some(svc) = tonic_service(arg) {
                        let mut rec = Record::new(
                            "rpc",
                            svc.clone(),
                            self.path,
                            start_line(i.method.span()),
                            self.enclosing(),
                        );
                        rec.attrs = attrs(&[
                            ("framework", json!("tonic")),
                            ("service", json!(expr_render(arg))),
                        ]);
                        self.out.records.push(rec);
                    }
                }
            }
            "register_method" | "register_async_method"
            | "register_blocking_method" | "register_subscription" => {
                if let Some(name) = i.args.first().and_then(lit_str) {
                    let handler = i
                        .args
                        .get(1)
                        .and_then(|a| expr_path(a).and_then(path_tail))
                        .unwrap_or_else(|| self.enclosing());
                    let mut rec = Record::new(
                        "rpc",
                        name,
                        self.path,
                        start_line(i.method.span()),
                        handler,
                    );
                    rec.attrs = attrs(&[("framework", json!("jsonrpsee"))]);
                    self.out.records.push(rec);
                }
            }
            _ => {}
        }
        visit::visit_expr_method_call(self, i);
    }
}

/// Render a full path (`a::b::X`); kept for parity with other modules.
#[allow(dead_code)]
fn full_path(p: &syn::Path) -> String {
    path_string(p)
}

#[cfg(test)]
mod tests {
    use crate::testutil::{ids, run};

    #[test]
    fn jsonrpsee_methods_and_implicit_names() {
        let out = run(
            r#"
#[rpc(server, namespace = "eth")]
pub trait EthApi {
    #[method(name = "eth_getProof")]
    async fn get_proof(&self);
    async fn block_number(&self);
    #[subscription(name = "eth_subscribe", item = String)]
    async fn subscribe(&self);
}
"#,
        );
        let rpc = ids(&out, "rpc");
        assert!(rpc.contains(&"eth_getProof".to_string()));
        assert!(rpc.contains(&"eth_block_number".to_string()));
        assert!(rpc.contains(&"eth_subscribe".to_string()));
    }

    #[test]
    fn tonic_add_service_and_register_method() {
        let out = run(
            r#"
pub fn serve(b: Builder) {
    b.add_service(greeter::GreeterServer::new(svc))
        .add_service(EthApiServer::new(api));
}
fn manual(m: &mut RpcModule) {
    m.register_method("debug_traceCall", h).unwrap();
}
"#,
        );
        let rpc = ids(&out, "rpc");
        assert!(rpc.contains(&"Greeter".to_string()));
        assert!(rpc.contains(&"EthApi".to_string()));
        assert!(rpc.contains(&"debug_traceCall".to_string()));
    }
}
