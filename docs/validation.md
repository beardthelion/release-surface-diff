# Validation: real-repo gates

Manual gates for the spec's test plan items 3-5 (plan U10). They need
network and large clones, so they run by hand before the plan is declared
done, not in CI. The synthetic e2e (`tests/test_e2e_synthetic.py`, spec
item 2) and watch idempotence (spec item 6, covered by
`tests/test_watch.py::test_second_run_no_new_tag_does_no_work`) are the
automated half.

Each gate below has a procedure, a checklist, and an empty result table.
Fill the table on the run and record the commit, tag pair, and machine
alongside it.

## Gate 1: coder/coder adjacent minor tags (route recall)

Spec item 3: on two adjacent minor tags, the tool must recall at least
90% of the manually listed new routes with at most 10% noise.

### Procedure

1. Pick two adjacent minor tags (for example `v2.X.0` and `v2.(X+1).0`)
   and clone: `git clone --filter=blob:none https://github.com/coder/coder`
2. Build the expected list by hand from two sources:
   - the minor release's notes/changelog (new endpoints called out), and
   - the router enumeration in `coderd/coderd.go` (and files it mounts):
     diff the registration calls between the tags and list every added
     `r.Get`/`r.Post`/`r.Route`/`r.Mount` path, normalized to
     `METHOD /template/{param}` form.
3. Run:

   ```
   release-surface-diff run --repo <coder clone> --from v2.X.0 --to v2.(X+1).0 --lang go --out out/
   ```

4. Compare `out/coder/coder/v2.X.0..v2.(X+1).0/surface.json`:
   - recall = (expected ids present in `added` + `renamed.after` + `changed.after`) / expected count
   - noise = `added` ids not on the expected list / `added` count
   - treat `{id}`-style template differences on the same path as matches;
     count anything else literally.

### Checklist

- [ ] Tag pair recorded (from/to) and clone commit noted
- [ ] Expected route list written down before looking at surface.json
- [ ] Recall >= 90%
- [ ] Noise <= 10%
- [ ] Disagreements classified: extractor miss vs manual-list error
- [ ] `report.md` gap notes reviewed (missing tools, partial coverage)

### Results

| Field | Value |
|---|---|
| Date / machine | |
| Tag pair (from -> to) | |
| Clone commit | |
| Expected new routes (count) | |
| Recalled | |
| Recall % | |
| surface.json `added` count | |
| Noisy `added` ids | |
| Noise % | |
| Pass / fail | |

Misses observed:

| Expected id | Seen as | Notes |
|---|---|---|
| | | |

## Gate 2: reth minor tag pair (jsonrpsee methods + CLI args)

Spec item 4: on one reth minor tag pair, new jsonrpsee methods and new
CLI args must appear in `surface.json`; 10 spot-checked guard hunks must
carry the right class.

### Procedure

1. Clone: `git clone --filter=blob:none https://github.com/paradigmxyz/reth`
2. Pick a minor tag pair (for example `v1.Y.0` and `v1.(Y+1).0`).
3. Expected list, before running the tool:
   - new RPC methods: diff the `#[rpc(namespace = ...)]` /
     `#[method(name = "...")]` attributes under `crates/rpc*` between
     tags and list added method names;
   - new CLI args: diff the clap structs (`#[arg]`/`#[command]` derives,
     typically under `bin/reth` and `crates/node/core`) and list added
     flags in `--kebab-case` form.
4. Run:

   ```
   release-surface-diff run --repo <reth clone> --from v1.Y.0 --to v1.(Y+1).0 --lang rust --out out/
   ```

5. Assert every expected method/flag id appears in `surface.json`
   `added` (or `renamed.after`/`changed.after`).
6. Spot-check 10 hunks from `guards.json`: for each, open the hunk range
   in the from/to worktree (or `git show`) and confirm the assigned
   `class` and `direction` match the pattern rules in `docs/SPEC.md`.

### Checklist

- [ ] Tag pair recorded; rust-extractor binary used (from `bin/` or env)
- [ ] Expected jsonrpsee method list written down first
- [ ] Expected CLI arg list written down first
- [ ] All expected methods/flags present in `added`
- [ ] 10 guard hunks spot-checked against the spec's class rules
- [ ] Misses attributed: extractor gap vs expected-list error

### Results

| Field | Value |
|---|---|
| Date / machine | |
| Tag pair (from -> to) | |
| Expected new jsonrpsee methods | |
| Found in `added` | |
| Expected new CLI args | |
| Found in `added` | |
| Guard hunks checked | |
| Correct class/direction | |
| Pass / fail | |

Spot-check detail:

| # | File : lines | Class | Direction | Correct? | Notes |
|---|---|---|---|---|---|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |
| 4 | | | | | |
| 5 | | | | | |
| 6 | | | | | |
| 7 | | | | | |
| 8 | | | | | |
| 9 | | | | | |
| 10 | | | | | |

## Gate 3: historic advisory regression (Go repo guard change)

Spec item 5: a known historic guard addition or removal from a published
advisory fix in a Go repo must appear in `guards.json` with the right
class and direction.

### Procedure

1. Pick a published Go advisory whose fix commit adds or removes a guard
   (bounds check, auth check, allowlist entry). Candidates: a Go
   standard-library fix (golang/go tags), containerd, runc, or Coder
   advisories. Record the advisory ID, the fix commit, and the tag pair
   bracketing it (`--from` = last tag before the fix, `--to` = first tag
   containing it; the fix may share the pair with other commits, which
   is fine, the hunk only needs to appear).
2. Note the expected `class` and `direction` from reading the patch.
3. Run:

   ```
   release-surface-diff run --repo <clone> --from <t1> --to <t2> --lang go --out out/
   ```

4. Confirm `guards.json` contains a hunk whose `file`/line range covers
   the advisory change with the expected `class` and `direction`, and
   that `seeds.jsonl` carries the same hunk with `source: release-diff`.

### Checklist

- [ ] Advisory ID and fix commit recorded
- [ ] Expected class/direction written down first
- [ ] Hunk present in guards.json with correct class and direction
- [ ] Corresponding seed present with correct file/symbol/evidence
- [ ] `sha`/`parent` attribution sanity-checked (`git log -L` result or
      tag-commit fallback)

### Results

| Field | Value |
|---|---|
| Date / machine | |
| Repo / advisory ID | |
| Fix commit | |
| Tag pair (from -> to) | |
| Expected class / direction | |
| Observed class / direction | |
| guards.json hunk (file, lines) | |
| seeds.jsonl line present | |
| Pass / fail | |

## Open item: seeds.jsonl consumer

`seeds.jsonl` targets the cve-variant-sweep silent-fix seed contract, but
that consumer is not available on this machine, so the file is verified
only for shape (one line per guard hunk, spec field set). When a real
consumer exists, run it against the seeds from one of the gates above
and record the result here.
