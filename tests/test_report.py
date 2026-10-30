"""U7: report.md sections - entry delta, ranked hunks, SCOPE, coverage."""

import re

from release_surface_diff.guards import GuardHunk
from release_surface_diff.rank import rank_hunks
from release_surface_diff.report import build_report, shape_tags


def hunk(symbol="", cls="bounds", direction="removed", file="srv/api.go",
         evidence="- if len(b) > maxLen {"):
    return GuardHunk(
        file=file, old_file=file, old_start=40, old_count=6,
        new_start=40, new_count=6, hunk_class=cls, direction=direction,
        symbol=symbol, evidence=evidence, context=None,
        changed=[("-", evidence.lstrip("-+ "))],
    )


def surface():
    return {
        "repo": "owner/repo",
        "from": "v1.0.0",
        "to": "v1.1.0",
        "generated_at": "2026-10-29T00:00:00Z",
        "added": [
            {
                "kind": "route", "id": "POST /decode", "file": "srv/api.go",
                "line": 12, "symbol": "decodeFoo", "lang": "go",
                "attrs": {},
            },
            {
                "kind": "route", "id": "ANY /health", "file": "srv/health.go",
                "line": 3, "symbol": "health", "lang": "go",
                "attrs": {"low_confidence": True},
            },
        ],
        "removed": [
            {
                "kind": "cli_flag", "id": "--legacy", "file": "cmd/main.go",
                "line": 8, "symbol": "main", "lang": "go", "attrs": {},
            }
        ],
        "changed": [],
        "renamed": [],
    }


def make_report(hunks=None):
    surf = surface()
    ranked = rank_hunks(hunks or [], surf, [])
    return build_report(
        surf, ranked, ["egress-plane not found on PATH"],
        repo="owner/repo", from_ref="v1.0.0", to_ref="v1.1.0",
        generated_at="2026-10-29T00:00:00Z",
    )


class TestSections:
    def test_all_sections_present(self):
        text = make_report()
        for section in (
            "## Entry-point delta",
            "### Added (2)",
            "### Removed (0)",  # cli_flag is not an entry-point kind
            "## Guard hunks (ranked)",
            "## SCOPE",
            "## Coverage",
        ):
            assert section in text, section

    def test_added_route_in_scope_and_coverage(self):
        text = make_report()
        scope = text.split("## SCOPE", 1)[1].split("## Coverage")[0]
        assert "- POST /decode" in scope
        assert "[ ] POST /decode" in text
        assert "uncovered" in text

    def test_gap_notes_render(self):
        assert "egress-plane not found on PATH" in make_report()

    def test_low_confidence_marked(self):
        assert "low confidence" in make_report()


class TestShapeTags:
    def test_length_field_tag_from_bounds_evidence(self):
        rec = surface()["added"][0]
        ranked = rank_hunks(
            [hunk(symbol="decodeFoo")], surface(), []
        )
        assert "length-field" in shape_tags(rec, ranked)

    def test_dispatch_tag(self):
        rec = surface()["added"][0]
        ranked = rank_hunks(
            [
                hunk(symbol="decodeFoo", cls="dispatch",
                     evidence="+ case OpRead: handle(r)"),
                hunk(symbol="decodeFoo", cls="dispatch",
                     evidence="+ case OpWrite: handle(w)"),
            ],
            surface(), [],
        )
        tags = shape_tags(rec, ranked)
        assert any(t.startswith("dispatch=") for t in tags)

    def test_no_tags_without_evidence(self):
        rec = surface()["added"][1]
        assert shape_tags(rec, []) == []


class TestDeterminism:
    def test_same_input_same_output(self):
        assert make_report([hunk()]) == make_report([hunk()])
