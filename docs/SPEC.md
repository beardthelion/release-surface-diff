# release-surface-diff: spec

## Problem

Fresh code is the least-audited code. Today the surface extractors (egress-plane, acl-guard-audit `scan`) list entry points for one snapshot. Nothing compares two releases, so new routes, CLI flags, RPC methods, config keys, and exports land unnoticed, and added or removed guards hide inside large release diffs.

## Goals

- For a target repo and a tag pair (N-1, N), emit every entry point that was added, removed, or changed.
- Narrow the code diff to security-relevant hunks: added or removed checks, limits, bounds, auth guards, allowlists, size caps.
- Cover Go and Rust first (consensus clients, coder, forgejo, sysbox class targets), then Python and TS.
- Emit machine-readable output that feeds the harness scope-boundary step, the coverage ledger, and cve-variant-sweep seeds.
- Watch mode: on each new tag, run against the previous tag automatically.

## Non-goals

- Finding bugs. This tool picks where to look; it does not judge exploitability.
- Running target code or tests.
- Replacing the existing extractors. It calls them and adds extractors only where they have no coverage.
- Web recon (subdomains, JS endpoint crawling).

## Inputs

- `--repo <path|url>`: git repo (cloned shallow-by-tag if a URL).
- `--from <tag>` / `--to <tag>`, or `--latest` (newest tag vs the one before by semver order, ignoring pre-releases unless `--pre`).
- `--lang go,rust,python,ts` (default: autodetect from manifests).
- `--include`/`--exclude` path globs (default excludes tests, vendored code, generated code, docs).

## Outputs

- `out/<repo>/<from>..<to>/surface.json`: entry-point delta.
- `out/<repo>/<from>..<to>/guards.json`: security-relevant hunk list.
- `out/<repo>/<from>..<to>/report.md`: human summary, ranked.
- `out/<repo>/<from>..<to>/seeds.jsonl`: one line per guard hunk, in the seed format below, for cve-variant-sweep.
- Exit codes: 0 no delta, 1 delta found, 2 tool error.

## Architecture

```
git worktree (from) ─┐
                     ├─► extractors ─► normalize ─► diff ─► rank ─► surface.json
git worktree (to)  ──┘                                   
git diff from..to ─► hunk classifier ─► guards.json, seeds.jsonl
```

### Extractors (reuse first)

| Surface kind | Source | Reuse |
|---|---|---|
| Outbound routes (HTTP clients, sockets, DNS) in Rust, Python, JS/TS, Go | egress-plane | Run `egress-plane scan --format json <root>` (CLI in `crates/egress-cli/src/main.rs`, scanners in `crates/egress-go`, `crates/egress-rust`, `crates/egress-python`, `crates/egress-js`). Treat JSON as a black box; key each item by file, symbol, destination kind. |
| HTTP handlers in Python and TS | acl-guard-audit | Run `acl-guard-audit scan <root>` (`src/acl_guard_audit/cli.py`, route discovery in `src/acl_guard_audit/routes.py`, `src/acl_guard_audit/openapi.py`). Use only the handler inventory, not the finding verdicts. |
| Go HTTP routes, gRPC/JSON-RPC methods, cobra/flag CLI flags, exported funcs, config struct tags | new | Go extractor using `go/packages` + `go/ast`: `http.HandleFunc`, `mux.Handle*`, chi/gin/echo/gorilla registrations, `RegisterService` descs, `rpc.Register`, geth-style `apis []rpc.API` namespaces, `cobra.Command` + `Flags().*`, `flag.*`, `urfave/cli` `Flag` literals, struct fields with `toml`/`yaml`/`json`/`mapstructure` tags reachable from a config root, exported identifiers in non-internal packages. |
| Rust HTTP routes, RPC methods, CLI flags, config keys, pub API | new | Rust extractor using `syn`: axum/actix/warp/tonic route and service registration, jsonrpsee `#[rpc]` / `#[method(name=...)]`, clap derive (`#[arg]`, `#[command]`), serde config structs (`#[serde(rename)]` honored), `pub` items in non-`pub(crate)` modules. libp2p protocol IDs (`StreamProtocol::new`, `/eth2/...` strings) as a surface kind. |
| Gossip topics, req/resp protocol IDs (consensus clients) | new | String-literal pass for `/eth2/beacon_chain/req/` and gossip topic templates; cheap, high value for nimbus, grandine, reth, lighthouse class targets. |

Every extractor emits the same normalized record:

```json
{"kind":"route|rpc|cli_flag|config_key|export|outbound|protocol_id",
 "id":"stable key, e.g. GET /api/v2/users/{id} or eth_getProof or --http.api",
 "file":"path","line":123,"symbol":"enclosing fn",
 "lang":"go","extractor":"go-ast@0.1",
 "attrs":{"auth_hint":"middleware names on the route chain if visible"}}
```

### Diff

- Match records by `(kind, id)`. Added, removed, or changed (same id, different handler symbol, middleware chain, or file).
- For changed handlers, attach the function body diff.
- Rename detection: same handler body hash, new id, counts as `renamed` not add+remove.

### Guard hunk classifier

Input: `git diff -U5 from..to` restricted to non-test paths. A hunk is security-relevant when an added or removed line matches one of:

- comparison against a length, size, count, depth, or limit (`len(` / `.len()` / `MAX_` / `limit` / `max_`), or a new early return or error on such a comparison;
- auth or permission calls (`authorize`, `check_perm`, `require_*`, `is_admin`, `verify`, middleware added or removed on a route);
- allowlist or denylist edits, `match`/`switch` arms added to a dispatch;
- `checked_*`, `saturating_*`, overflow checks, `unwrap` removal, bounds checks before indexing;
- path, URL, or host validation (`Clean`, `canonicalize`, `is_private`, scheme checks);
- removed lines of any of the above score higher than added lines (a removed guard is a regression candidate).

Each hunk gets `class` (`bounds|auth|allowlist|overflow|path|dispatch|other`), `direction` (`added|removed|modified`), and the enclosing symbol. Hunks inside a function that is also a surface delta rank first.

### Ranking

score = surface delta weight (new route/RPC/protocol 3, new flag/config 2, export 1) + guard hunk weight (removed guard 3, added guard 2, modified 1) + reach bonus (hunk symbol is called from a new or changed entry point, via a cheap same-package call graph).

## Data formats

`seeds.jsonl` (shared with the cve-variant-sweep silent-fix seed source):

```json
{"repo":"owner/name","sha":"<to-tag commit or the commit that introduced the hunk>","parent":"<sha>","source":"release-diff","class":"bounds","files":["x.go"],"symbol":"decodeFoo","evidence":"+ if len(b) > maxFoo {"}
```

Introducing commit: resolve by `git log -L` on the hunk range between the tags; fall back to the tag commit.

`surface.json`: `{"repo","from","to","generated_at","added":[record],"removed":[record],"changed":[{"before":record,"after":record,"body_diff":"..."}],"renamed":[...]}`.

## Harness integration

- `report.md` has a `SCOPE` section listing new entry points as candidate lines for the scope-boundary step, with shape tags guessed from the guard class (`length-field` for bounds on decoded length, `dispatch=<handled>/<upstream enum>` when a dispatch arm count changed, `amplify` for new loops over attacker counts).
- A `coverage` section lists each new entry point as an uncovered row, so the coverage ledger can mark it audited or excluded.
- The tool stays program-agnostic: no bounty program names or rules in code or output.

## CLI

```
release-surface-diff run --repo ../reth --from v1.4.0 --to v1.5.0 [--lang rust] [--out out/]
release-surface-diff run --repo https://github.com/coder/coder --latest
release-surface-diff watch --config targets.toml        # check each target for a new tag, run once per new tag, keep state in state.json
release-surface-diff extract --root <tree> --lang go    # single snapshot inventory, for debugging an extractor
```

`targets.toml`: `[[target]] repo="..." lang=["go"] include=[...] exclude=[...]`.

## Implementation notes

- Language: Python 3.11 orchestrator (matches cve-variant-sweep and acl-guard-audit), small Go helper binary for the Go extractor, small Rust helper binary for the Rust extractor. Each helper reads a root and writes normalized JSONL to stdout.
- Use `git worktree add` per tag, never checkout in place. Clean up on exit.
- Missing optional tools (egress-plane, acl-guard-audit, semgrep) degrade to a warning and a gap note in `report.md`, never a silent skip.
- Deterministic output ordering so two runs diff cleanly.

## Test plan

1. Unit: each extractor against small fixture trees in `tests/fixtures/<lang>/` with a known record list (golden JSON).
2. Diff: synthetic two-commit fixture with one added route, one removed flag, one renamed handler, one removed bounds check. Assert exact surface.json and guards.json.
3. Validation target (real): coder/coder, two adjacent minor tags. Manually list new routes from the release notes and the `coderd/coderd.go` router; tool recall must be at least 90 percent of that list with no more than 10 percent noise.
4. Validation target (Rust/consensus): one reth minor tag pair. Assert new jsonrpsee methods and new CLI args appear; spot check 10 guard hunks for correct class.
5. Regression: a known historic guard removal or addition from a published advisory fix in a Go repo must appear in guards.json with the right class and direction.
6. Idempotence: `watch` run twice with no new tag does no work and exits 0.

## Milestones

1. M1: orchestrator, git worktrees, egress-plane and acl-guard-audit adapters, normalized record, diff, surface.json. Fixture tests.
2. M2: Go extractor (routes, gRPC/JSON-RPC, cobra/flag, config tags, exports). coder validation.
3. M3: guard hunk classifier, guards.json, seeds.jsonl, ranking, report.md.
4. M4: Rust extractor (axum/actix/tonic/jsonrpsee/clap/serde/libp2p). reth validation.
5. M5: watch mode with state, targets.toml, harness SCOPE and coverage sections.
