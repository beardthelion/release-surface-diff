"""U7: rank_hunks scoring and ordering."""

from release_surface_diff.guards import GuardHunk
from release_surface_diff.rank import (
    REACH_BONUS,
    reachable_symbols,
    rank_delta,
    rank_hunks,
    surface_weight,
)
from release_surface_diff.records import CallEdge


def hunk(symbol="", cls="bounds", direction="removed", file="a.go", start=10):
    return GuardHunk(
        file=file,
        old_file=file,
        old_start=start,
        old_count=4,
        new_start=start,
        new_count=4,
        hunk_class=cls,
        direction=direction,
        symbol=symbol,
        evidence="- if len(b) > max {",
    )


def surface_with(**delta):
    return {"added": [], "removed": [], "changed": [], "renamed": [], **delta}


class TestWeights:
    def test_kind_weights_match_spec(self):
        assert surface_weight("route") == 3
        assert surface_weight("rpc") == 3
        assert surface_weight("protocol_id") == 3
        assert surface_weight("cli_flag") == 2
        assert surface_weight("config_key") == 2
        assert surface_weight("export") == 1
        assert surface_weight("outbound") == 1

    def test_direction_weights_order(self):
        surface = surface_with()
        ranked = rank_hunks(
            [
                hunk(direction="added"),
                hunk(direction="modified"),
                hunk(direction="removed"),
            ],
            surface,
        )
        assert [r.hunk.direction for r in ranked] == [
            "removed", "added", "modified",
        ]

    def test_delta_symbol_ranks_first(self):
        rec = {
            "kind": "route", "id": "GET /a", "file": "a.go",
            "symbol": "decodeFoo", "line": 10,
        }
        surface = surface_with(added=[rec])
        ranked = rank_hunks(
            [hunk(symbol="other"), hunk(symbol="decodeFoo")], surface
        )
        assert ranked[0].hunk.symbol == "decodeFoo"
        assert ranked[0].in_delta_symbol


class TestReach:
    def test_reach_bonus_from_entry_point(self):
        rec = {
            "kind": "route", "id": "GET /a", "file": "a.go",
            "symbol": "handler", "line": 1,
        }
        surface = surface_with(added=[rec])
        edges = [CallEdge(caller="handler", callee="parseBody")]
        assert "parseBody" in reachable_symbols(edges, {"handler"})
        ranked = rank_hunks(
            [hunk(symbol="parseBody"), hunk(symbol="unrelated")],
            surface,
            edges,
        )
        assert ranked[0].hunk.symbol == "parseBody"
        assert ranked[0].reach_bonus == REACH_BONUS

    def test_no_edges_no_bonus(self):
        surface = surface_with(
            added=[{"kind": "route", "id": "GET /a", "symbol": "h"}]
        )
        ranked = rank_hunks([hunk(symbol="unrelated")], surface, [])
        assert ranked[0].reach_bonus == 0


class TestRankDelta:
    def test_buckets_sort_by_weight_then_id(self):
        surface = surface_with(
            added=[
                {"kind": "export", "id": "B", "file": "x.go"},
                {"kind": "route", "id": "GET /b", "file": "x.go"},
                {"kind": "route", "id": "GET /a", "file": "x.go"},
            ]
        )
        ranked = rank_delta(surface)
        assert [r["id"] for r in ranked["added"]] == [
            "GET /a", "GET /b", "B",
        ]

    def test_changed_pairs_sort_by_after_record(self):
        surface = surface_with(
            changed=[
                {
                    "before": {"kind": "export", "id": "A"},
                    "after": {"kind": "export", "id": "A"},
                },
                {
                    "before": {"kind": "route", "id": "GET /x"},
                    "after": {"kind": "route", "id": "GET /x"},
                },
            ]
        )
        ranked = rank_delta(surface)
        assert ranked["changed"][0]["after"]["kind"] == "route"
