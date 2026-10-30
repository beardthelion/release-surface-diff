//! Golden-fixture test: run the built binary on
//! tests/fixtures/rust and compare stdout plus both sidecars to the
//! checked-in golden JSONL.

use std::fs;
use std::path::PathBuf;
use std::process::Command;

fn fixture() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/rust")
        .canonicalize()
        .expect("rust fixture tree")
}

fn read_lines(path: &PathBuf) -> Vec<String> {
    fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .map(|l| l.to_string())
        .collect()
}

#[test]
fn golden_fixture_records_symbols_edges() {
    let root = fixture();
    let tmp = std::env::temp_dir().join(format!("rsd-u8-golden-{}", std::process::id()));
    let _ = fs::remove_dir_all(&tmp);
    fs::create_dir_all(&tmp).unwrap();
    let syms = tmp.join("symbols.jsonl");
    let edges = tmp.join("edges.jsonl");

    let out = Command::new(env!("CARGO_BIN_EXE_rust-extractor"))
        .arg(&root)
        .arg("--symbols")
        .arg(&syms)
        .arg("--edges")
        .arg(&edges)
        .output()
        .expect("run rust-extractor");
    assert!(out.status.success());
    let stderr = String::from_utf8_lossy(&out.stderr);
    // the intentionally broken file degrades to a warning, not a crash
    assert!(stderr.contains("broken.rs"), "stderr was: {}", stderr);

    let stdout = String::from_utf8_lossy(&out.stdout);
    let golden_dir = root.join("golden");

    let got_records: Vec<String> = stdout.lines().map(|l| l.to_string()).collect();
    let want_records = read_lines(&golden_dir.join("records.jsonl"));
    assert_eq!(got_records, want_records, "records mismatch");

    assert_eq!(
        read_lines(&syms),
        read_lines(&golden_dir.join("symbols.jsonl")),
        "symbols mismatch"
    );
    assert_eq!(
        read_lines(&edges),
        read_lines(&golden_dir.join("edges.jsonl")),
        "edges mismatch"
    );
}
