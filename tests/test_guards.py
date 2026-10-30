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
    # both auth (removed) and bounds (added) lines; bounds outranks auth and
    # direction is computed on the winning class's lines only
    [h] = guards.classify_diff(MINI)
    assert h.hunk_class == "bounds"
    assert h.direction == "added"
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
    # removed guards attribute to the commit that introduced the lines on
    # the from side (c2), not the to-tag commit (c4)
    assert found["decode.py"]["sha"] == shas["c2"]
    assert found["decode.py"]["parent"] == shas["c1"]
    assert found["flags.py"]["sha"] == shas["c2"]
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
