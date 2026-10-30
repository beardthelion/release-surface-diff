"""Guard classifier and seeds tests: hunk parsing, class precedence,
direction, symbol attribution, path filtering, and log -L attribution
with tag-commit fallback (R13, R15; KTD10, KTD11)."""

import json
import subprocess
from pathlib import Path

import pytest

from release_surface_diff import gitops, guards, seeds
from release_surface_diff.records import SymbolSpan

FIXTURES = Path(__file__).parent / "fixtures" / "guards"
GENERATED_AT = "2026-10-29T00:00:00Z"


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "config", "tag.gpgsign", "false")
    return path


def write_all(repo: Path, files: dict, message: str) -> str:
    for name, content in files.items():
        (repo / name).write_text(content)
    git(repo, "add", *sorted(files))
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD").strip()


BASELINE = {
    "decode.py": "def decode_foo(buf):\n    return parse(buf)\n",
    "flags.py": "def add_flags(parser):\n    parser.add_argument(\"--verbose\")\n",
    "handlers.py": "def admin_stats(request):\n    return {\"stats\": True}\n",
    "routes.py": (
        "def register_routes(app):\n"
        "    app.get(\"/health\", health)\n"
        "    app.get(\"/admin\", admin_stats)\n"
    ),
}

GUARDED = {
    **BASELINE,
    "decode.py": (
        "def decode_foo(buf):\n"
        "    if len(buf) > MAX_FOO:\n"
        "        raise ValueError(\"too big\")\n"
        "    return parse(buf)\n"
    ),
    "flags.py": (
        "def add_flags(parser):\n"
        "    parser.add_argument(\"--allowlist-file\", help=\"comma-separated issuers\")\n"
        "    parser.add_argument(\"--verbose\")\n"
    ),
}

RELEASED = {
    **BASELINE,
    "handlers.py": (
        "def admin_overview(request):\n"
        "    return {\"stats\": True, \"ok\": 1}\n"
    ),
    "routes.py": (
        "def register_routes(app):\n"
        "    app.get(\"/health\", health)\n"
        "    app.get(\"/admin\", require_admin(admin_overview))\n"
        "    app.get(\"/metrics\", metrics)\n"
    ),
}

SYMBOLS = [
    SymbolSpan(file="decode.py", symbol="decode_foo", start_line=1, end_line=4),
    SymbolSpan(file="flags.py", symbol="add_flags", start_line=1, end_line=3),
    SymbolSpan(file="handlers.py", symbol="admin_stats", start_line=1, end_line=2),
    SymbolSpan(file="routes.py", symbol="register_routes", start_line=1, end_line=3),
]


@pytest.fixture
def synthetic_repo(tmp_path: Path):
    """The spec's synthetic pair, made so the guard additions predate the
    from tag: c1 baseline, c2 adds the decode/flags guards, c2b tagged
    v1.0.0, c4 releases the changes and is annotated-tagged v1.1.0."""
    repo = init_repo(tmp_path / "target")
    c1 = write_all(repo, BASELINE, "baseline")
    c2 = write_all(repo, GUARDED, "add guards")
    c2b = write_all(
        repo,
        {**GUARDED, "handlers.py": "def admin_stats(request):\n"
         "    return {\"stats\": True, \"ok\": 1}\n"},
        "unrelated tweak",
    )
    git(repo, "tag", "v1.0.0")
    c4 = write_all(repo, RELEASED, "release changes")
    git(repo, "tag", "-a", "v1.1.0", "-m", "release v1.1.0")
    return repo, {"c1": c1, "c2": c2, "c2b": c2b, "c4": c4}


def classify(repo: Path, symbols=SYMBOLS):
    diff_text = gitops.git_diff(repo, "v1.0.0", "v1.1.0")
    return guards.classify_diff(diff_text, symbols=symbols)


# ---- hunk classification ----


def test_synthetic_guards_json(synthetic_repo) -> None:
    repo, _ = synthetic_repo
    hunks = classify(repo)
    doc = guards.guards_json(
        hunks, repo="target", from_ref="v1.0.0", to_ref="v1.1.0",
        generated_at=GENERATED_AT,
    )
    golden = json.loads((FIXTURES / "guards_synthetic.json").read_text())
    assert doc == golden


def test_synthetic_hunk_details(synthetic_repo) -> None:
    repo, _ = synthetic_repo
    hunks = classify(repo)
    by_file = {h.file: h for h in hunks}

    # three security-relevant hunks; the handlers.py rename matched nothing
    assert [h.file for h in hunks] == ["decode.py", "flags.py", "routes.py"]

    d = by_file["decode.py"]
    assert (d.hunk_class, d.direction, d.symbol) == ("bounds", "removed", "decode_foo")
    assert d.evidence == "-    if len(buf) > MAX_FOO:"
    assert d.weight == 3

    f = by_file["flags.py"]
    assert (f.hunk_class, f.direction, f.symbol) == ("allowlist", "removed", "add_flags")
    assert "--allowlist-file" in f.evidence

    r = by_file["routes.py"]
    assert (r.hunk_class, r.direction, r.symbol) == ("auth", "added", "register_routes")
    assert r.evidence == '+    app.get("/admin", require_admin(admin_overview))'
    assert r.weight == 2


MINI = """\
diff --git a/x.py b/x.py
index 111..222 100644
--- a/x.py
+++ b/x.py
@@ -1,3 +1,4 @@ def f():
 ctx
-    check_perm(u)
+    if len(b) > max_len:
+        return err
"""


def test_precedence_first_class_wins() -> None:
    # both auth (removed) and bounds (added) lines; bounds outranks auth for
    # the class label, but direction counts every matched line so a removed
    # guard of any class cannot be masked as a pure addition
    [h] = guards.classify_diff(MINI)
    assert h.hunk_class == "bounds"
    assert h.direction == "modified"
    assert h.evidence == "+    if len(b) > max_len:"


def test_modified_direction() -> None:
    diff = MINI.replace("-    check_perm(u)\n+    if len(b) > max_len:\n+        return err",
                        "-    if check_perm(u):\n+    if require_user(u):")
    [h] = guards.classify_diff(diff)
    assert h.hunk_class == "auth"
    assert h.direction == "modified"
    assert h.evidence == "-    if check_perm(u):"


def test_context_fallback_symbol() -> None:
    [h] = guards.classify_diff(MINI, symbols=[])
    assert h.symbol == "def f():"


def test_comment_only_lines_not_classified() -> None:
    # guard-shaped text inside comments carries no class/direction
    diff = """\
diff --git a/x.go b/x.go
index 1..2 100644
--- a/x.go
+++ b/x.go
@@ -1,3 +1,5 @@
 ctx
-// check_perm(u) was here
+// check_perm(u) is gone
+/* len(b) > max_len */
+* canonicalize(p)
 ctx2
"""
    assert guards.classify_diff(diff) == []
    # a real change alongside a matching comment still classifies
    diff2 = diff.replace(" ctx2\n", "+    check_perm(u)\n ctx2\n")
    [h] = guards.classify_diff(diff2)
    assert h.direction == "added"
    assert h.evidence == "+    check_perm(u)"


def test_langs_filter_skips_other_lang_files() -> None:
    file_diff = """\
diff --git a/{p} b/{p}
index 1..2 100644
--- a/{p}
+++ b/{p}
@@ -1,1 +1,2 @@
 ctx
+    check_perm(u)
"""
    diff = "".join(
        file_diff.format(p=p)
        for p in ("deploy.yaml", "main.go", "app.py", "Makefile", "note.ps1")
    )
    hunks = guards.classify_diff(diff, langs={"go"})
    # .py maps to another lang and drops; .yaml/.ps1 and no-extension
    # files have no EXT_LANG mapping and stay in scope
    assert sorted(h.file for h in hunks) == [
        "Makefile", "deploy.yaml", "main.go", "note.ps1",
    ]
    hunks_all = guards.classify_diff(diff)
    assert len(hunks_all) == 5


def test_excluded_paths_not_classified() -> None:
    file_diff = """\
diff --git a/{p} b/{p}
index 1..2 100644
--- a/{p}
+++ b/{p}
@@ -1,1 +1,2 @@
 ctx
+    check_perm(u)
"""
    diff = "".join(
        file_diff.format(p=p)
        for p in (
            "tests/test_app.py", "src/foo_test.go", "vendor/dep/x.go",
            "pkg/gen/api.pb.go", "pkg/generated/api.py", "docs/notes.md",
            "pkg/app.py",
        )
    )
    hunks = guards.classify_diff(diff)
    assert [h.file for h in hunks] == ["pkg/app.py"]


def test_evidence_truncated_at_200() -> None:
    long_line = "x" * 250
    diff = f"""\
diff --git a/x.py b/x.py
index 1..2 100644
--- a/x.py
+++ b/x.py
@@ -1,1 +1,2 @@
 ctx
+    if len(buf) > MAX_{long_line}:
"""
    [h] = guards.classify_diff(diff)
    assert h.hunk_class == "bounds"
    assert len(h.evidence) == 200
    assert h.evidence.startswith("+")


def test_hunks_keep_diff_order() -> None:
    diff = """\
diff --git a/z.py b/z.py
index 1..2 100644
--- a/z.py
+++ b/z.py
@@ -1,1 +1,2 @@
 a
+    check_perm(u)
@@ -10,1 +10,2 @@
 b
+    verify_sig(m)
diff --git a/a.py b/a.py
index 1..2 100644
--- a/a.py
+++ b/a.py
@@ -1,1 +1,2 @@
 c
+    deny_list.add(x)
"""
    hunks = guards.classify_diff(diff)
    assert [(h.file, h.new_start) for h in hunks] == [
        ("z.py", 1), ("z.py", 10), ("a.py", 1),
    ]


@pytest.mark.parametrize(
    "line,hunk_class,direction",
    [
        ("+    n = n.wrapping_add(1)", "overflow", "added"),
        ("-    p = p.canonicalize()", "path", "removed"),
        ("+    case OpRead: handle(r)", "dispatch", "added"),
        ("-    deny(req)", "other", "removed"),
    ],
)
def test_classify_diff_representative_lines(line, hunk_class, direction):
    old_n, new_n = (2, 1) if line.startswith("-") else (1, 2)
    diff = f"""\
diff --git a/x.py b/x.py
index 1..2 100644
--- a/x.py
+++ b/x.py
@@ -1,{old_n} +1,{new_n} @@
 ctx
{line}
"""
    [h] = guards.classify_diff(diff)
    assert h.hunk_class == hunk_class
    assert h.direction == direction
    assert h.evidence == line


def test_deleted_file_classifies_on_old_path() -> None:
    diff = """\
diff --git a/x.py b/x.py
index 1..2 100644
deleted file mode 100644
--- a/x.py
+++ /dev/null
@@ -1,2 +0,0 @@
 ctx
-    check_perm(u)
"""
    [h] = guards.classify_diff(diff)
    assert h.file == "x.py"
    assert h.old_file == "x.py"
    assert h.hunk_class == "auth"
    assert h.direction == "removed"


def test_created_file_classifies_on_new_path() -> None:
    diff = """\
diff --git a/x.py b/x.py
new file mode 100644
index 0..2
--- /dev/null
+++ b/x.py
@@ -0,0 +1,2 @@
+ctx
+    check_perm(u)
"""
    [h] = guards.classify_diff(diff)
    assert h.file == "x.py"
    assert h.old_file == "x.py"
    assert h.hunk_class == "auth"
    assert h.direction == "added"


def test_quoted_path_header_unquotes() -> None:
    diff = (
        'diff --git "a/h\\303\\251llo.py" "b/h\\303\\251llo.py"\n'
        "index 1..2 100644\n"
        '--- "a/h\\303\\251llo.py"\n'
        '+++ "b/h\\303\\251llo.py"\n'
        "@@ -1,1 +1,2 @@\n"
        " ctx\n"
        "+    check_perm(u)\n"
    )
    [h] = guards.classify_diff(diff)
    assert h.file == "hÃ©llo.py"


def test_guards_json_roundtrip(synthetic_repo, tmp_path) -> None:
    repo, _ = synthetic_repo
    doc = guards.guards_json(
        classify(repo), repo="target", from_ref="v1.0.0", to_ref="v1.1.0",
        generated_at=GENERATED_AT,
    )
    out = guards.write_guards_json(doc, tmp_path / "out" / "guards.json")
    assert json.loads(out.read_text()) == doc


# ---- seeds.jsonl ----


def test_seed_shape_and_fields(synthetic_repo) -> None:
    repo, _ = synthetic_repo
    hunks = classify(repo)
    lines = seeds.build_seeds(repo, str(repo), hunks, "v1.0.0", "v1.1.0")
    assert len(lines) == len(hunks)
    expected_keys = {
        "repo", "sha", "parent", "source", "class", "files", "symbol", "evidence",
    }
    for seed, hunk in zip(lines, hunks):
        assert set(seed) == expected_keys
        assert seed["source"] == "release-diff"
        assert seed["repo"] == gitops.repo_slug(str(repo))
        assert seed["class"] == hunk.hunk_class
        assert seed["files"] == [hunk.file]
        assert seed["symbol"] == hunk.symbol
        assert seed["evidence"] == hunk.evidence
        assert len(seed["sha"]) == 40


def test_log_l_resolves_introducing_commit(synthetic_repo) -> None:
    repo, shas = synthetic_repo
    hunks = classify(repo)
    found = {h.file: s for h, s in zip(
        hunks, seeds.build_seeds(repo, str(repo), hunks, "v1.0.0", "v1.1.0")
    )}
    # removed guards attribute to the commit that removed the lines within
    # the tag range: c4, which is also the to-tag commit here (R15)
    assert found["decode.py"]["sha"] == shas["c4"]
    assert found["decode.py"]["parent"] == shas["c2b"]
    assert found["flags.py"]["sha"] == shas["c4"]
    # added lines attribute on the to side; c4 is the tag commit itself
    assert found["routes.py"]["sha"] == shas["c4"]
    assert found["routes.py"]["parent"] == shas["c2b"]


def test_log_l_fallback_for_unknown_path(synthetic_repo) -> None:
    repo, shas = synthetic_repo
    hunk = guards.GuardHunk(
        file="ghost.py", old_file="ghost.py",
        old_start=1, old_count=2, new_start=0, new_count=0,
        hunk_class="auth", direction="removed",
        symbol="f", evidence="-    check_perm(u)",
    )
    [seed] = seeds.build_seeds(repo, str(repo), [hunk], "v1.0.0", "v1.1.0")
    assert seed["sha"] == shas["c4"]          # tag commit (annotated tag peeled)
    assert seed["parent"] == shas["c2b"]      # the tag commit's parent


def test_lookup_cap_falls_back_to_tag(synthetic_repo) -> None:
    repo, shas = synthetic_repo
    hunks = classify(repo)
    found = seeds.build_seeds(
        repo, str(repo), hunks, "v1.0.0", "v1.1.0", max_lookups=0
    )
    assert all(s["sha"] == shas["c4"] for s in found)


def test_seeds_jsonl_roundtrip(synthetic_repo, tmp_path) -> None:
    repo, _ = synthetic_repo
    hunks = classify(repo)
    lines = seeds.build_seeds(repo, str(repo), hunks, "v1.0.0", "v1.1.0")
    out = seeds.write_seeds_jsonl(lines, tmp_path / "out" / "seeds.jsonl")
    parsed = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
    assert parsed == lines


def test_removed_hunk_seed_attributes_to_remover(tmp_path) -> None:
    """Three commits: c1 adds a guard, c2 unrelated, c3 removes it. The seed
    sha for the removed hunk is the removing commit c3, parent is c2."""
    import subprocess
    from release_surface_diff import seeds as seeds_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a],
                              capture_output=True, text=True, check=True).stdout
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "app.py").write_text(
        "def h():\n    check_perm(u)\n    return 1\n")
    git("add", "app.py"); git("commit", "-qm", "c1")
    c1 = git("rev-parse", "HEAD").strip()
    git("tag", "v1.0.0")
    (repo / "other.txt").write_text("x")
    git("add", "other.txt"); git("commit", "-qm", "c2")
    c2 = git("rev-parse", "HEAD").strip()
    (repo / "app.py").write_text("def h():\n    return 1\n")
    git("add", "app.py"); git("commit", "-qm", "c3")
    c3 = git("rev-parse", "HEAD").strip()
    git("tag", "v1.1.0")

    hunk = guards.GuardHunk(
        file="app.py", old_file="app.py",
        old_start=2, old_count=1, new_start=1, new_count=1,
        hunk_class="auth", direction="removed", symbol=None,
        evidence="-    check_perm(u)")
    out = seeds_mod.build_seeds(repo, str(repo), [hunk], "v1.0.0", "v1.1.0")
    assert out[0]["sha"] == c3
    assert out[0]["parent"] == c2


def _seed_repo(tmp_path, extra=None):
    """c1 guard at lines 10-11 (v1.0.0); c2 removes it; c3 touches the same
    file post-removal (optionally shifting content); c4 touches another file
    (v1.1.0)."""
    import subprocess
    repo = tmp_path / "repo"; repo.mkdir()
    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a],
                              capture_output=True, text=True, check=True).stdout
    git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    body = [f"line{i}" for i in range(1, 10)] + ["check_perm(u)", "len(b) > max_"] + [f"line{i}" for i in range(12, 15)]
    (repo / "x.py").write_text("\n".join(body) + "\n")
    git("add", "x.py"); git("commit", "-qm", "c1"); c1 = git("rev-parse", "HEAD").strip()
    git("tag", "v1.0.0")
    (repo / "x.py").write_text("\n".join(body[:9] + body[11:]) + "\n")
    git("add", "x.py"); git("commit", "-qm", "c2"); c2 = git("rev-parse", "HEAD").strip()
    (repo / "x.py").write_text("\n".join(extra(body) if extra else body[:9] + body[11:13] + ["edited"] + body[13:]) + "\n")
    git("add", "x.py"); git("commit", "-qm", "c3"); c3 = git("rev-parse", "HEAD").strip()
    (repo / "y.py").write_text("other\n")
    git("add", "y.py"); git("commit", "-qm", "c4"); c4 = git("rev-parse", "HEAD").strip()
    git("tag", "v1.1.0")
    hunk = guards.GuardHunk(
        file="x.py", old_file="x.py", old_start=10, old_count=2,
        new_start=10, new_count=0, hunk_class="auth", direction="removed",
        symbol=None, evidence="-check_perm(u)")
    return repo, git, c1, c2, c3, c4, hunk


def test_removed_seed_attributes_to_remover_not_to_toucher(tmp_path) -> None:
    """The repro: -L at the tip would hit c3's unrelated edit at old line 11;
    reverse-blame attribution must land on c2."""
    from release_surface_diff import seeds as seeds_mod
    repo, _git, c1, c2, c3, c4, hunk = _seed_repo(tmp_path)
    [seed] = seeds_mod.build_seeds(repo, str(repo), [hunk], "v1.0.0", "v1.1.0")
    assert seed["sha"] == c2
    assert seed["parent"] == c1
    assert "attribution" not in seed


def test_removed_seed_with_shifting_lines_after_removal(tmp_path) -> None:
    """c3 inserts lines above the old guard position; attribution must still
    land on c2 via the from-side range, not the shifted to-side lines."""
    from release_surface_diff import seeds as seeds_mod
    def shift(body):
        return ["new0", "new1", "new2"] + body[:9] + body[11:13] + ["edited"] + body[13:]
    repo, _git, c1, c2, c3, c4, hunk = _seed_repo(tmp_path, extra=shift)
    [seed] = seeds_mod.build_seeds(repo, str(repo), [hunk], "v1.0.0", "v1.1.0")
    assert seed["sha"] == c2
    assert seed["parent"] == c1


def test_merge_base_attribution_diverged_tags(tmp_path) -> None:
    """Release-branch pair: v1.0.0 on one branch, v1.1.0 on a second
    branch off the same base. from is not an ancestor of to, so
    attribution must run over merge-base..to: the removed hunk lands on
    the divergent commit that rewrote that region, the added hunk on its
    introducing commit, with no tag fallback."""
    from release_surface_diff import seeds as seeds_mod

    repo = init_repo(tmp_path / "repo")
    base_body = (
        ["def f():"]
        + [f"    x{i:02d}" for i in range(1, 29)]
        + ["    return 1"]
    )
    write_all(repo, {"app.py": "\n".join(base_body) + "\n"}, "base")
    base = git(repo, "rev-parse", "HEAD").strip()

    # branch one: insert a guard near the top, tag v1.0.0
    git(repo, "checkout", "-qb", "rel-a")
    body_a = base_body[:10] + ["    check_perm(u)"] + base_body[10:]
    write_all(repo, {"app.py": "\n".join(body_a) + "\n"}, "add guard")
    git(repo, "tag", "v1.0.0")

    # branch two off the base: rewrite the same region (removing those
    # base lines), then add a different guard near the bottom
    git(repo, "checkout", "-qb", "rel-b", base)
    body_b = (
        base_body[:9]
        + ["    rewritten = 2"]
        + base_body[12:]
        + ["    if len(b) > MAX_B:", "        raise"]
    )
    write_all(repo, {"app.py": "\n".join(body_b) + "\n"}, "rewrite region")
    d1 = git(repo, "rev-parse", "HEAD").strip()
    body_b2 = body_b[:-2] + ["    if len(b) > MAX_C:", "        raise"] + body_b[-2:]
    write_all(repo, {"app.py": "\n".join(body_b2) + "\n"}, "add guard")
    d2 = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "tag", "v1.1.0")

    assert git(repo, "merge-base", "v1.0.0", "v1.1.0").strip() == base

    diff_text = gitops.git_diff(repo, "v1.0.0", "v1.1.0")
    hunks = guards.classify_diff(diff_text)
    assert len(hunks) == 2  # the removed guard and the added guard
    seeds_out = seeds_mod.build_seeds(
        repo, str(repo), hunks, "v1.0.0", "v1.1.0"
    )
    by_dir = {h.direction: (h, s) for h, s in zip(hunks, seeds_out)}
    removed, added = by_dir["removed"], by_dir["added"]
    assert removed[1]["sha"] == d1          # the region-rewriting commit
    assert added[1]["sha"] == d2            # the guard-introducing commit
    assert "attribution" not in removed[1]  # no tag fallback
    assert "attribution" not in added[1]


def test_seed_repo_uses_origin_owner_name(tmp_path) -> None:
    """Local --repo with an origin remote yields owner/name, not basename."""
    from release_surface_diff import seeds as seeds_mod
    repo, git, c1, c2, c3, c4, hunk = _seed_repo(tmp_path)
    git("remote", "add", "origin", "https://github.com/coder/coder.git")
    [seed] = seeds_mod.build_seeds(repo, str(repo), [hunk], "v1.0.0", "v1.1.0")
    assert seed["repo"] == "coder/coder"


def test_seed_repo_falls_back_to_dirname(tmp_path) -> None:
    """No remote: dir name is used, never an empty string."""
    from release_surface_diff import seeds as seeds_mod
    repo, git, c1, c2, c3, c4, hunk = _seed_repo(tmp_path)
    [seed] = seeds_mod.build_seeds(repo, str(repo), [hunk], "v1.0.0", "v1.1.0")
    assert seed["repo"] == "repo"
