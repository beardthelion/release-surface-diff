//! Test-only helper: run every extractor over one source string.

use crate::model::Extraction;

pub fn run(src: &str) -> Extraction {
    let file = syn::parse_file(src).expect("test source must parse");
    let mut out = Extraction::default();
    let defined = crate::symbols::extract(&file, "t.rs", &mut out);
    crate::calls::extract(&file, "t.rs", &defined, &mut out);
    crate::routes::extract(&file, "t.rs", &mut out);
    crate::rpc::extract(&file, "t.rs", &mut out);
    crate::cli::extract(&file, "t.rs", &mut out);
    crate::config::extract(&file, "t.rs", &mut out);
    crate::exports::extract(&file, "t.rs", &mut out);
    crate::protocols::extract(&file, "t.rs", &mut out);
    out.finalize();
    out
}

pub fn ids(out: &Extraction, kind: &str) -> Vec<String> {
    let mut v: Vec<String> = out
        .records
        .iter()
        .filter(|r| r.kind == kind)
        .map(|r| r.id.clone())
        .collect();
    v.sort();
    v.dedup();
    v
}
