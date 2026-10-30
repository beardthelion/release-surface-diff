//! Symbol-table sidecar (KTD7): every named item emits
//! (file, symbol, start_line, end_line) with the same qualified naming the
//! call-edge pass and the record extractors use, so guard hunks attribute
//! to the right body and renamed-body hashing sees one scheme.

use std::collections::{HashMap, HashSet};

use syn::spanned::Spanned;
use syn::visit::{self, Visit};
use syn::{File, Item, ItemImpl, ItemMod, ItemTrait, Type};

use crate::model::{Extraction, SymbolSpan};
use crate::util::{end_line, path_tail, start_line, Scope};

/// Index of every qualified name defined in one file; call-edge
/// resolution looks candidates up here and drops unresolvable edges
/// (KTD8).
#[derive(Debug, Default)]
pub struct DefinedIndex {
    pub qualified: HashSet<String>,
    pub by_tail: HashMap<String, Vec<String>>,
}

impl DefinedIndex {
    fn insert(&mut self, qualified: String) {
        if let Some(tail) = qualified.rsplit("::").next() {
            self.by_tail
                .entry(tail.to_string())
                .or_default()
                .push(qualified.clone());
        }
        self.qualified.insert(qualified);
    }

    /// Resolve a written name to a defined qualified name.
    ///
    /// `mod_prefix` is the caller's `a::b::` module prefix. Order:
    /// same-module, file root, then a unique `::name` suffix match.
    pub fn resolve(&self, written: &str, mod_prefix: &str) -> Option<String> {
        let same_mod = format!("{}{}", mod_prefix, written);
        if self.qualified.contains(&same_mod) {
            return Some(same_mod);
        }
        if self.qualified.contains(written) {
            return Some(written.to_string());
        }
        if let Some(tail) = written.rsplit("::").next() {
            let cands: Vec<&String> = self
                .by_tail
                .get(tail)
                .map(|v| v.iter().filter(|q| q.ends_with(written)).collect())
                .unwrap_or_default();
            if cands.len() == 1 {
                return Some(cands[0].clone());
            }
        }
        None
    }
}

pub fn extract(file: &File, path: &str, out: &mut Extraction) -> DefinedIndex {
    let mut v = SymbolVisit {
        path,
        out,
        defined: DefinedIndex::default(),
        scope: Scope::default(),
    };
    v.visit_file(file);
    v.defined
}

struct SymbolVisit<'a> {
    path: &'a str,
    out: &'a mut Extraction,
    defined: DefinedIndex,
    scope: Scope,
}

impl SymbolVisit<'_> {
    fn emit(&mut self, name: &str, node: &impl Spanned) {
        let span = node.span();
        let qualified = self.scope.qualify(name);
        self.defined.insert(qualified.clone());
        self.out.symbols.push(SymbolSpan {
            file: self.path.to_string(),
            symbol: qualified,
            start_line: start_line(span),
            end_line: end_line(span),
        });
    }
}

impl<'ast> Visit<'ast> for SymbolVisit<'ast> {
    fn visit_item(&mut self, item: &'ast Item) {
        let name: Option<String> = match item {
            Item::Fn(i) => Some(i.sig.ident.to_string()),
            Item::Struct(i) => Some(i.ident.to_string()),
            Item::Enum(i) => Some(i.ident.to_string()),
            Item::Union(i) => Some(i.ident.to_string()),
            Item::Const(i) => Some(i.ident.to_string()),
            Item::Static(i) => Some(i.ident.to_string()),
            Item::Type(i) => Some(i.ident.to_string()),
            // trait and mod handle their own scoping below
            Item::Trait(_) | Item::Mod(_) | Item::Impl(_) => None,
            _ => None,
        };
        if let Some(name) = name {
            self.emit(&name, item);
        }
        visit::visit_item(self, item);
    }

    fn visit_item_mod(&mut self, i: &'ast ItemMod) {
        let name = i.ident.to_string();
        self.emit(&name, i);
        self.scope.mods.push(name);
        visit::visit_item_mod(self, i);
        self.scope.mods.pop();
    }

    fn visit_item_trait(&mut self, i: &'ast ItemTrait) {
        let name = i.ident.to_string();
        self.emit(&name, i);
        self.scope.imp.push(name);
        visit::visit_item_trait(self, i);
        self.scope.imp.pop();
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

    fn visit_impl_item_fn(&mut self, i: &'ast syn::ImplItemFn) {
        self.emit(&i.sig.ident.to_string(), i);
        visit::visit_impl_item_fn(self, i);
    }

    fn visit_trait_item_fn(&mut self, i: &'ast syn::TraitItemFn) {
        self.emit(&i.sig.ident.to_string(), i);
        visit::visit_trait_item_fn(self, i);
    }
}

/// `Foo` for `impl Foo`, `a::Foo`, `Box<Foo>`, `impl dyn Trait` etc.
pub fn type_name(ty: &Type) -> Option<String> {
    match ty {
        Type::Path(tp) => path_tail(&tp.path),
        Type::TraitObject(_) | Type::Reference(_) | Type::Paren(_) | Type::Group(_) => None,
        _ => None,
    }
}

