//! rust-extractor: Rust release-surface extraction for
//! release-surface-diff (U8, R9, R11).
//!
//! Contract (adapters/helpers.py):
//!
//!     rust-extractor <root> --symbols <path> --edges <path>
//!
//! Normalized records go to stdout as JSONL; the symbol table (KTD7) is
//! written at --symbols and intra-module call edges (KTD8) at --edges,
//! both as JSONL. Files that fail `syn` parsing are skipped with an
//! stderr warning; extraction continues.

mod calls;
mod cli;
mod config;
mod exports;
mod model;
mod protocols;
mod routes;
mod rpc;
mod symbols;
#[cfg(test)]
mod testutil;
mod util;

use std::env;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use model::Extraction;

fn usage() -> ExitCode {
    eprintln!("usage: rust-extractor <root> [--symbols <path>] [--edges <path>]");
    ExitCode::from(2)
}

/// Recursive .rs walk; skips hidden dirs and `target/`. Sorted for
/// deterministic output (R16).
fn walk(dir: &Path, files: &mut Vec<PathBuf>) {
    let Ok(rd) = fs::read_dir(dir) else { return };
    let mut entries: Vec<fs::DirEntry> = rd.flatten().collect();
    entries.sort_by_key(|e| e.path());
    for e in entries {
        let Ok(ft) = e.file_type() else { continue };
        if ft.is_symlink() {
            // A symlinked dir can point back into the tree and recurse
            // forever; symlinked files are skipped for the same reason.
            continue;
        }
        let p = e.path();
        if ft.is_dir() {
            let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if name.starts_with('.') || name == "target" {
                continue;
            }
            walk(&p, files);
        } else if p.extension().and_then(|e| e.to_str()) == Some("rs") {
            files.push(p);
        }
    }
}

fn rel(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}

fn extract_file(root: &Path, path: &Path, out: &mut Extraction) {
    let rel = rel(root, path);
    let Ok(src) = fs::read_to_string(path) else {
        out.warnings
            .push(format!("{}: could not read file", rel));
        return;
    };
    // syn's parser recurses per nesting level, so a pathological file
    // (generated corpus fixtures nest 100k+) overflows any stack before
    // visitors run. Cheap byte scan bounds the parse instead.
    const MAX_NESTING: usize = 4096;
    let mut depth = 0usize;
    for b in src.bytes() {
        match b {
            b'(' | b'[' | b'{' => {
                depth += 1;
                if depth > MAX_NESTING {
                    out.warnings.push(format!(
                        "{}: skipped (nesting depth > {})",
                        rel, MAX_NESTING
                    ));
                    return;
                }
            }
            b')' | b']' | b'}' => depth = depth.saturating_sub(1),
            _ => {}
        }
    }
    let file = match syn::parse_file(&src) {
        Ok(f) => f,
        Err(e) => {
            out.warnings
                .push(format!("{}: syn parse failed: {}", rel, e));
            return;
        }
    };
    let defined = symbols::extract(&file, &rel, out);
    calls::extract(&file, &rel, &defined, out);
    routes::extract(&file, &rel, out);
    rpc::extract(&file, &rel, out);
    cli::extract(&file, &rel, out);
    config::extract(&file, &rel, out);
    exports::extract(&file, &rel, out);
    protocols::extract(&file, &rel, out);
}

fn write_sidecar(path: Option<&PathBuf>, lines: impl Iterator<Item = String>) -> Result<(), String> {
    let Some(path) = path else { return Ok(()) };
    let mut buf = String::new();
    for l in lines {
        buf.push_str(&l);
        buf.push('\n');
    }
    fs::write(path, buf).map_err(|e| format!("could not write {}: {}", path.display(), e))
}

const WORK_STACK_BYTES: usize = 256 * 1024 * 1024;

fn main() -> ExitCode {
    // Extraction runs on a spawned thread with a large stack: deeply nested
    // real-world files overflow the default 8 MiB stack and abort the
    // process, and the thread boundary also turns ordinary panics into a
    // clean exit 2 instead of a crash.
    let args: Vec<String> = env::args().skip(1).collect();
    match std::thread::Builder::new()
        .stack_size(WORK_STACK_BYTES)
        .spawn(move || run(args))
    {
        Ok(h) => match h.join() {
            Ok(code) => code,
            Err(_) => {
                eprintln!("rust-extractor: worker panicked");
                ExitCode::from(2)
            }
        },
        Err(e) => {
            eprintln!("rust-extractor: could not spawn worker: {e}");
            ExitCode::from(2)
        }
    }
}

fn run(args: Vec<String>) -> ExitCode {
    let mut root: Option<PathBuf> = None;
    let mut symbols: Option<PathBuf> = None;
    let mut edges: Option<PathBuf> = None;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--symbols" => {
                i += 1;
                symbols = args.get(i).map(PathBuf::from);
            }
            "--edges" => {
                i += 1;
                edges = args.get(i).map(PathBuf::from);
            }
            other => {
                if root.is_none() && !other.starts_with("--") {
                    root = Some(PathBuf::from(other));
                } else {
                    eprintln!("rust-extractor: unexpected argument {}", other);
                    return usage();
                }
            }
        }
        i += 1;
    }
    let Some(root) = root else { return usage() };
    if !root.is_dir() {
        eprintln!("rust-extractor: {} is not a directory", root.display());
        return usage();
    }

    let mut files = Vec::new();
    walk(&root, &mut files);
    let mut out = Extraction::default();
    for f in &files {
        extract_file(&root, f, &mut out);
    }
    out.finalize();

    let stdout = std::io::stdout();
    let mut lock = stdout.lock();
    for r in &out.records {
        let _ = writeln!(lock, "{}", r.to_json());
    }
    let _ = lock.flush();

    if let Err(e) = write_sidecar(
        symbols.as_ref(),
        out.symbols.iter().map(|s| s.to_json()),
    ) {
        out.warnings.push(e);
    }
    if let Err(e) = write_sidecar(edges.as_ref(), out.edges.iter().map(|e| e.to_json()))
    {
        out.warnings.push(e);
    }
    for w in &out.warnings {
        eprintln!("rust-extractor: warn: {}", w);
    }
    ExitCode::SUCCESS
}
