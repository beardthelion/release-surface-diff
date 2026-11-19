# release-surface-diff

Compares two release tags of a repository and lists the attack surface that was added, removed, changed or renamed between them, plus the diff hunks that add or remove security checks. The output is a ranked report of where to look first in a new release.

## Why

New code is the least audited code. Release diffs are large, and new routes, RPC methods, CLI flags and config keys slip in unnoticed, as do removed bounds or auth checks. This tool inventories the externally reachable entry points on both tags, diffs them, and pulls out the guard changes so a reviewer starts from a short list instead of the whole diff. It picks places to look. It does not judge exploitability and never runs target code.

## What it extracts

| Surface | Languages | Source |
|---|---|---|
| HTTP routes, gRPC and JSON-RPC methods, cobra/flag/urfave CLI flags, tagged config struct fields, exported identifiers | Go | `helpers/go-extractor` (go/ast) |
| axum, actix, warp and tonic routes, jsonrpsee methods, clap args, serde config keys, `pub` items, libp2p protocol IDs | Rust | `helpers/rust-extractor` (syn) |
| HTTP routes (Flask, FastAPI, Django, Express-style routers, Fastify, Next.js route files) | Python, TS/JS | built in |
| req/resp protocol IDs and gossip topic strings | all | built in |
| Outbound network calls | Go, Rust, Python, JS/TS | [egress-plane](https://github.com/beardthelion/egress-plane), optional |
| Python/TS handler inventory | Python, TS | `acl-guard-audit` binary, optional (not public yet) |

The Go and Rust helpers are built into `bin/` on first use, and rebuilt when their sources change, if `go` or `cargo` is on PATH. Any missing tool degrades to a warning plus a gap note in the report, never a silent skip.

Guard hunks come from `git diff -U5 from..to` on non-test paths. A hunk counts when an added or removed line matches one of these families, checked in this order: `bounds`, `auth`, `allowlist`, `overflow`, `path`, `dispatch`, `other`. Each hunk gets a direction (`added`, `removed`, `modified`) and the enclosing symbol. Removed guards score highest.

## Install

Python 3.11 or newer, no runtime Python dependencies.

```bash
git clone https://github.com/beardthelion/release-surface-diff
cd release-surface-diff
pip install -e '.[test]'
release-surface-diff --help
```

For Go and Rust extraction you need `go` and/or `cargo` on PATH.

## Usage

```bash
# Two explicit tags
release-surface-diff run --repo ../somerepo --from v1.4.0 --to v1.5.0 --lang rust --out out/

# Newest tag against the previous one (semver order, pre-releases skipped unless --pre)
release-surface-diff run --repo https://github.com/org/somerepo --latest

# One tree's normalized inventory as JSONL, for checking an extractor
release-surface-diff extract --root ../somerepo --lang go

# One-shot check of a target list for new tags; schedule it yourself
release-surface-diff watch --config targets.toml
```

`--lang` defaults to autodetection from manifests in the newer tree. `--include` and `--exclude` take repo-relative globs; the default excludes tests, scripts, vendored, generated and doc trees. External tools resolve from a flag, then an environment variable (`RSD_EGRESS_PLANE`, `RSD_ACL_GUARD_AUDIT`, `RSD_GO_EXTRACTOR`, `RSD_RUST_EXTRACTOR`), then PATH.

`targets.toml` for `watch` holds `[[target]]` entries with `repo`, and optionally `lang`, `include` and `exclude`. State is kept in `state.json` next to it (or at `state_file`). A failed or degraded run does not advance the stored tag, so it retries next time.

Exit codes for `run`: `0` no delta, `1` delta found, `2` tool error. `watch` exits `0` when the check completes and `2` when a target run fails or the config is invalid.

## Output

`run` writes to `out/<repo>/<from>..<to>/`:

- `surface.json`: `added`, `removed`, `changed` (before and after records) and `renamed` entry points. Each record has `kind` (`route`, `rpc`, `cli_flag`, `config_key`, `export`, `outbound`, `protocol_id`), `id`, `file`, `line`, `symbol`, `lang`, `extractor`, `attrs`.
- `guards.json`: classified guard hunks with class, direction, file, line range, symbol, evidence line and weight.
- `seeds.jsonl`: one line per guard hunk, with the introducing commit (`git log -L`, falling back to the tag commit), for variant hunting with [cve-variant-sweep](https://github.com/beardthelion/cve-variant-sweep).
- `report.md`: ranked summary with `SCOPE` and `Coverage` sections that can be pasted into an audit's scope and coverage ledgers.

Example on a two-tag Go repo where `v1.1.0` added a route and dropped a length check:

```
$ release-surface-diff run --repo demo --latest --out out
v1.0.0..v1.1.0: outputs in out/demo/v1.0.0..v1.1.0
$ echo $?
1
$ cat out/demo/v1.0.0..v1.1.0/report.md
...
### Added (1)

- `ANY /api/admin` (main.go:8, weight 3)
...
## Guard hunks (ranked)

- 3 | bounds removed | main.go:1 | users | -	if len(b) > 1024 { return nil }
```

Output ordering is deterministic, so two runs diff cleanly.

## Tests

```bash
pytest -q
```

The Go and Rust helper tests build the helpers and need `go` and a default `cargo` toolchain. The real-repo validation gates in [docs/validation.md](docs/validation.md) (route recall on a large Go service, RPC and CLI recall on a Rust client, a known advisory guard change) are written up as procedures; their result tables have not been filled in yet.

## Limitations

- Pattern-based: the guard classifier matches line patterns, so it misses guards that do not look like one and flags some lines that are not.
- TS/JS routes are found with pattern rules, not a TypeScript parser.
- The call graph used for ranking is a cheap same-package one.
- Symbol attribution falls back to the diff's `@@` context when the symbol tables do not cover a hunk, which can name the wrong function.
- No web recon (subdomains, crawled JS endpoints).

Design notes: [docs/SPEC.md](docs/SPEC.md).

## Related tools

- [egress-plane](https://github.com/beardthelion/egress-plane): outbound-call inventory used as one extractor
- [cve-variant-sweep](https://github.com/beardthelion/cve-variant-sweep): consumes `seeds.jsonl`
- [fuzzgen](https://github.com/beardthelion/fuzzgen): fuzz the new parser entry points this tool surfaces
- [autocatalytic-harness](https://github.com/beardthelion/autocatalytic-harness): the audit harness the `SCOPE` and `Coverage` sections feed
