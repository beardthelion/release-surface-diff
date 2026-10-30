//! clap derive CLI surface (R9): `#[arg(long)]`/`#[command]` fields on
//! `Parser`/`Args` structs emit `--name` cli_flag records; `Subcommand`
//! enum variants emit `--subcommand` records.

use serde_json::json;
use syn::visit::{self, Visit};
use syn::{Expr, File, Fields, ItemEnum, ItemStruct};

use crate::model::{attrs, Extraction, Record};
use crate::util::{
    assign_parts, attr_exprs, attr_name, derives, expr_path, kebab, lit_str, path_tail,
    start_line, Scope,
};

const PARSER_DERIVES: &[&str] = &["Parser", "Args", "Command", "Clap"];
const SUBCOMMAND_DERIVES: &[&str] = &["Subcommand"];
const CLAP_ATTRS: &[&str] = &["arg", "clap", "command"];

pub fn extract(file: &File, path: &str, out: &mut Extraction) {
    let mut v = CliVisit {
        path,
        out,
        scope: Scope::default(),
    };
    v.visit_file(file);
}

struct CliVisit<'a> {
    path: &'a str,
    out: &'a mut Extraction,
    scope: Scope,
}

/// Long-flag name from `#[arg(...)]`/`#[clap(...)]`/`#[command(...)]`
/// args: explicit `long = "x"` wins, bare `long` derives from the field.
/// Returns (Some(name), _) or None when no long flag is declared.
fn long_flag(attrs: &[syn::Attribute], field: &str) -> Option<Option<String>> {
    for a in attrs {
        if !CLAP_ATTRS.contains(&attr_name(a).as_str()) {
            continue;
        }
        if let Some(exprs) = attr_exprs(a) {
            for e in &exprs {
                match e {
                    Expr::Path(p) => {
                        if expr_path(e).and_then(path_tail).as_deref() == Some("long") {
                            return Some(None);
                        }
                        let _ = p;
                    }
                    _ => {
                        if let Some((k, v)) = assign_parts(e) {
                            if k == "long" {
                                return Some(lit_str(v));
                            }
                        }
                    }
                }
            }
        }
    }
    let _ = field;
    None
}

fn has_skip(attrs: &[syn::Attribute]) -> bool {
    for a in attrs {
        if CLAP_ATTRS.contains(&attr_name(a).as_str()) {
            if let Some(exprs) = attr_exprs(a) {
                for e in &exprs {
                    if let Some(t) = expr_path(e).and_then(path_tail) {
                        if t == "skip" {
                            return true;
                        }
                    }
                }
            }
        }
    }
    false
}

/// Explicit `name = "x"` inside a clap attr (subcommand rename).
fn name_override(attrs: &[syn::Attribute]) -> Option<String> {
    for a in attrs {
        if CLAP_ATTRS.contains(&attr_name(a).as_str()) {
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

impl CliVisit<'_> {
    fn emit(&mut self, id: String, symbol: String, line: usize, role: &str) {
        let mut rec = Record::new("cli_flag", format!("--{}", id), self.path, line, symbol);
        rec.attrs = attrs(&[
            ("framework", json!("clap")),
            ("role", json!(role)),
        ]);
        self.out.records.push(rec);
    }
}

impl<'ast> Visit<'ast> for CliVisit<'ast> {
    fn visit_item_mod(&mut self, i: &'ast syn::ItemMod) {
        self.scope.mods.push(i.ident.to_string());
        visit::visit_item_mod(self, i);
        self.scope.mods.pop();
    }

    fn visit_item_struct(&mut self, i: &'ast ItemStruct) {
        if derives(&i.attrs, PARSER_DERIVES) {
            let st = i.ident.to_string();
            if let Fields::Named(fields) = &i.fields {
                for f in &fields.named {
                    let Some(ident) = &f.ident else { continue };
                    let fname = ident.to_string();
                    match long_flag(&f.attrs, &fname) {
                        Some(Some(name)) => self.emit(
                            name,
                            self.scope.qualify(&format!("{}::{}", st, fname)),
                            start_line(ident.span()),
                            "flag",
                        ),
                        Some(None) => self.emit(
                            kebab(&fname),
                            self.scope.qualify(&format!("{}::{}", st, fname)),
                            start_line(ident.span()),
                            "flag",
                        ),
                        None => {}
                    }
                }
            }
        }
        visit::visit_item_struct(self, i);
    }

    fn visit_item_enum(&mut self, i: &'ast ItemEnum) {
        if derives(&i.attrs, SUBCOMMAND_DERIVES)
            || (derives(&i.attrs, PARSER_DERIVES) && !i.variants.is_empty())
        {
            let en = i.ident.to_string();
            for v in &i.variants {
                if has_skip(&v.attrs) {
                    continue;
                }
                let vname = v.ident.to_string();
                let name = name_override(&v.attrs).unwrap_or_else(|| kebab(&vname));
                self.emit(
                    name,
                    self.scope.qualify(&format!("{}::{}", en, vname)),
                    start_line(v.ident.span()),
                    "subcommand",
                );
            }
        }
        visit::visit_item_enum(self, i);
    }
}

#[cfg(test)]
mod tests {
    use crate::testutil::{ids, run};

    #[test]
    fn clap_long_flags_and_subcommands() {
        let out = run(
            r#"
#[derive(clap::Parser)]
pub struct Cli {
    #[arg(long)]
    verbose: bool,
    #[arg(long = "output-dir", short)]
    out: String,
    #[arg(short)]
    jobs: usize,
    positional: String,
    #[command(subcommand)]
    cmd: Commands,
}
#[derive(clap::Subcommand)]
pub enum Commands {
    Scan,
    #[command(name = "diff-report")]
    DiffReport { repo: String },
}
"#,
        );
        let flags = ids(&out, "cli_flag");
        assert_eq!(
            flags,
            vec![
                "--diff-report".to_string(),
                "--output-dir".to_string(),
                "--scan".to_string(),
                "--verbose".to_string(),
            ]
        );
    }
}
