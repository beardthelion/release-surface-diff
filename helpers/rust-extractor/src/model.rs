//! Shared output shapes: normalized records (R11), the symbol-table
//! sidecar (KTD7), and intra-module call edges (KTD8).

use serde_json::{json, Map, Value};

/// Normalized record; field order in JSON is alphabetical because
/// serde_json's Map is a BTreeMap (matches the orchestrator's sort_keys
/// output, R16).
#[derive(Debug, Clone)]
pub struct Record {
    pub kind: &'static str,
    pub id: String,
    pub file: String,
    pub line: usize,
    pub symbol: String,
    pub attrs: Map<String, Value>,
}

impl Record {
    pub fn new(kind: &'static str, id: String, file: &str, line: usize, symbol: String) -> Self {
        Record {
            kind,
            id,
            file: file.to_string(),
            line,
            symbol,
            attrs: Map::new(),
        }
    }

    pub fn to_json(&self) -> String {
        serde_json::to_string(&json!({
            "kind": self.kind,
            "id": self.id,
            "file": self.file,
            "line": self.line,
            "symbol": self.symbol,
            "lang": "rust",
            "extractor": concat!("rust-extractor@", env!("CARGO_PKG_VERSION")),
            "attrs": self.attrs,
        }))
        .expect("record serialization")
    }
}

/// (file, symbol, start_line, end_line) span used for enclosing-symbol
/// attribution, body_diff extraction, and body hashing (KTD7).
#[derive(Debug, Clone)]
pub struct SymbolSpan {
    pub file: String,
    pub symbol: String,
    pub start_line: usize,
    pub end_line: usize,
}

impl SymbolSpan {
    pub fn to_json(&self) -> String {
        serde_json::to_string(&json!({
            "file": self.file,
            "symbol": self.symbol,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }))
        .expect("symbol serialization")
    }
}

/// (caller, callee) edge resolvable within the same module/file (KTD8).
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct CallEdge {
    pub caller: String,
    pub callee: String,
}

impl CallEdge {
    pub fn to_json(&self) -> String {
        serde_json::to_string(&json!({
            "caller": self.caller,
            "callee": self.callee,
        }))
        .expect("edge serialization")
    }
}

/// Accumulator for everything one tree yields.
#[derive(Debug, Default)]
pub struct Extraction {
    pub records: Vec<Record>,
    pub symbols: Vec<SymbolSpan>,
    pub edges: Vec<CallEdge>,
    /// stderr warnings; kept here so parse/collection failures degrade
    /// instead of crashing.
    pub warnings: Vec<String>,
}

impl Extraction {
    /// Deterministic ordering (R16) plus dedupe of exact duplicate
    /// records and edges.
    pub fn finalize(&mut self) {
        self.records
            .sort_by(|a, b| (&a.kind, &a.id, &a.file, a.line, &a.symbol).cmp(&(
                &b.kind, &b.id, &b.file, b.line, &b.symbol,
            )));
        self.records.dedup_by(|a, b| {
            a.kind == b.kind && a.id == b.id && a.file == b.file && a.line == b.line
                && a.symbol == b.symbol
        });
        self.symbols.sort_by(|a, b| {
            (&a.file, a.start_line, &a.symbol).cmp(&(&b.file, b.start_line, &b.symbol))
        });
        self.symbols
            .dedup_by(|a, b| a.file == b.file && a.symbol == b.symbol);
        self.edges.sort();
        self.edges.dedup();
    }
}

/// Attrs helper: build a Map from pairs.
pub fn attrs(pairs: &[(&str, Value)]) -> Map<String, Value> {
    let mut m = Map::new();
    for (k, v) in pairs {
        m.insert(k.to_string(), v.clone());
    }
    m
}

/// Attrs helper: a string list.
pub fn str_list(items: &[String]) -> Value {
    Value::Array(items.iter().map(|s| Value::String(s.clone())).collect())
}
