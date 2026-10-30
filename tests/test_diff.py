"""Diff engine: (kind, id) matching, changed/body_diff, rename detection,
surface.json emission (R12, R14, R16; KTD5, KTD6, KTD7)."""

import json
import random
from pathlib import Path

from release_surface_diff import diff, records

FIXTURES = Path(__file__).parent / "fixtures" / "diff"
GENERATED_AT = "2026-10-29T00:00:00Z"


def rec(kind="route", id="GET /x", file="f.py", line=1, symbol="h",
        attrs=None, lang="python"):
    return records.Record(
        kind=kind, id=id, file=file, line=line, symbol=symbol,
        lang=lang, extractor="test@0", attrs=dict(attrs or {}),
    )


def span(file, symbol, start, end):
    return records.SymbolSpan(file=file, symbol=symbol,
                              start_line=start, end_line=end)


def body(n, marker="x"):
    """A def header plus enough lines to total n non-blank lines."""
    return "\n".join(["def h():"] + [f"    {marker}{i} = {i}" for i in range(n - 1)])


def snap(recs, files=None, symbols=None, low=False):
    files = files or {}
    return diff.Snapshot(
        records=list(recs), symbols=list(symbols or []),
        read_file=files.get, low_confidence=low,
    )


def run(before, after):
    return diff.diff_snapshots(
        before, after, repo="acme/widget",
        from_ref="v1.0.0", to_ref="v1.1.0", generated_at=GENERATED_AT,
    )


def load_fixture_side(name):
    base = FIXTURES / name
    recs = [
        records.Record.from_dict(json.loads(line))
        for line in (FIXTURES / f"{name}_records.jsonl").read_text().splitlines()
        if line.strip()
    ]
    syms = [
        records.SymbolSpan(**json.loads(line))
        for line in (FIXTURES / f"{name}_symbols.jsonl").read_text().splitlines()
        if line.strip()
    ]

    def read_file(path, _base=base):
        f = _base / path
        return f.read_text() if f.is_file() else None

    return diff.Snapshot(records=recs, symbols=syms, read_file=read_file)


# ---- match / add / remove / change (R12) ----


def test_match_add_remove_unchanged():
    same = rec(id="GET /same")
    before = snap([same, rec(id="GET /gone")])
    after = snap([rec(id="GET /same"), rec(id="GET /new")])
    result = run(before, after)
    assert [r["id"] for r in result["added"]] == ["GET /new"]
    assert [r["id"] for r in result["removed"]] == ["GET /gone"]
    assert result["changed"] == []
    assert result["renamed"] == []


def test_body_drift_is_changed_with_body_diff():
    before = snap(
        [rec(id="POST /orders", file="o.py", symbol="h")],
        files={"o.py": "def h():\n    old_call()\n"},
        symbols=[span("o.py", "h", 1, 2)],
    )
    after = snap(
        [rec(id="POST /orders", file="o.py", symbol="h")],
        files={"o.py": "def h():\n    if len(x) > MAX:\n        bail()\n    old_call()\n"},
        symbols=[span("o.py", "h", 1, 4)],
    )
    result = run(before, after)
    assert result["added"] == [] and result["removed"] == []
    assert len(result["changed"]) == 1
    entry = result["changed"][0]
    assert entry["before"]["id"] == entry["after"]["id"] == "POST /orders"
    assert "+    if len(x) > MAX:" in entry["body_diff"]
    assert "-    old_call()" not in entry["body_diff"]  # context line, not removed


def test_whitespace_only_body_drift_is_not_changed():
    before = snap(
        [rec(id="GET /x", file="f.py", symbol="h")],
        files={"f.py": "def h():\n    a = 1\n    b = 2\n"},
        symbols=[span("f.py", "h", 1, 3)],
    )
    after = snap(
        [rec(id="GET /x", file="f.py", symbol="h")],
        files={"f.py": "def h():\n        a = 1\n\n        b = 2\n"},
        symbols=[span("f.py", "h", 1, 4)],
    )
    assert run(before, after)["changed"] == []


def test_symbol_change_is_changed():
    before = snap([rec(id="GET /x", symbol="old_h")])
    after = snap([rec(id="GET /x", symbol="new_h")])
    result = run(before, after)
    assert len(result["changed"]) == 1
    # No symbol-table ranges either side: no body_diff (KTD7).
    assert result["changed"][0]["body_diff"] is None


def test_middleware_chain_attr_is_changed():
    before = snap([rec(id="GET /x", attrs={"auth_hint": "requireAuth"})])
    after = snap([rec(id="GET /x", attrs={"auth_hint": "requireAdmin"})])
    result = run(before, after)
    assert len(result["changed"]) == 1


def test_file_move_is_changed():
    before = snap([rec(id="GET /x", file="a.py", symbol="h")])
    after = snap([rec(id="GET /x", file="b.py", symbol="h")])
    assert len(run(before, after)["changed"]) == 1


def test_line_shift_alone_is_not_changed():
    before = snap([rec(id="GET /x", line=3)])
    after = snap([rec(id="GET /x", line=9)])
    assert run(before, after)["changed"] == []


# ---- rename detection (KTD6) ----


def test_unique_body_hash_pair_is_renamed():
    before = snap(
        [rec(id="GET /old", file="a.py", symbol="h")],
        files={"a.py": body(8)},
        symbols=[span("a.py", "h", 1, 8)],
    )
    after = snap(
        [rec(id="GET /new", file="b.py", symbol="h")],
        files={"b.py": body(8)},
        symbols=[span("b.py", "h", 1, 8)],
    )
    result = run(before, after)
    assert result["added"] == [] and result["removed"] == []
    assert len(result["renamed"]) == 1
    pair = result["renamed"][0]
    assert pair["before"]["id"] == "GET /old"
    assert pair["after"]["id"] == "GET /new"


def test_rename_tie_falls_back_to_add_remove():
    shared = body(8)
    before = snap(
        [rec(id="GET /a", file="a.py", symbol="h"),
         rec(id="GET /b", file="b.py", symbol="h")],
        files={"a.py": shared, "b.py": shared},
        symbols=[span("a.py", "h", 1, 8), span("b.py", "h", 1, 8)],
    )
    after = snap(
        [rec(id="GET /c", file="c.py", symbol="h"),
         rec(id="GET /d", file="d.py", symbol="h")],
        files={"c.py": shared, "d.py": shared},
        symbols=[span("c.py", "h", 1, 8), span("d.py", "h", 1, 8)],
    )
    result = run(before, after)
    assert result["renamed"] == []
    assert [r["id"] for r in result["added"]] == ["GET /c", "GET /d"]
    assert [r["id"] for r in result["removed"]] == ["GET /a", "GET /b"]


def test_short_body_below_floor_is_not_renamed():
    before = snap(
        [rec(id="GET /old", file="a.py", symbol="h")],
        files={"a.py": body(7)},
        symbols=[span("a.py", "h", 1, 7)],
    )
    after = snap(
        [rec(id="GET /new", file="b.py", symbol="h")],
        files={"b.py": body(7)},
        symbols=[span("b.py", "h", 1, 7)],
    )
    result = run(before, after)
    assert result["renamed"] == []
    assert [r["id"] for r in result["added"]] == ["GET /new"]
    assert [r["id"] for r in result["removed"]] == ["GET /old"]


def test_record_without_symbol_span_cannot_rename():
    before = snap([rec(id="GET /old", file="a.py", symbol="h")])
    after = snap([rec(id="GET /new", file="b.py", symbol="h")])
    result = run(before, after)
    assert result["renamed"] == []
    assert [r["id"] for r in result["added"]] == ["GET /new"]
    assert [r["id"] for r in result["removed"]] == ["GET /old"]


# ---- residual (kind, id) collisions: ordinal disambiguation (KTD5) ----


def test_collision_pairs_by_file_symbol_ordinal():
    before = snap([
        rec(id="GET /dup", file="b.py", symbol="hB"),
        rec(id="GET /dup", file="a.py", symbol="hA"),
    ])
    after = snap([
        rec(id="GET /dup", file="b.py", symbol="hB"),
        rec(id="GET /dup", file="a.py", symbol="hA"),
    ])
    result = run(before, after)
    assert result["added"] == []
    assert result["removed"] == []
    assert result["changed"] == []


def test_collision_leftover_is_removed():
    before = snap([
        rec(id="GET /dup", file="a.py", symbol="hA"),
        rec(id="GET /dup", file="b.py", symbol="hB"),
    ])
    after = snap([rec(id="GET /dup", file="a.py", symbol="hA")])
    result = run(before, after)
    assert result["added"] == [] and result["changed"] == []
    assert [r["file"] for r in result["removed"]] == ["b.py"]


def test_collision_leftover_is_added():
    before = snap([rec(id="GET /dup", file="a.py", symbol="hA")])
    after = snap([
        rec(id="GET /dup", file="a.py", symbol="hA"),
        rec(id="GET /dup", file="b.py", symbol="hB"),
    ])
    result = run(before, after)
    assert result["removed"] == [] and result["changed"] == []
    assert [r["file"] for r in result["added"]] == ["b.py"]


# ---- degraded side (R17 / KTD3) ----


def test_empty_from_side_everything_added_low_confidence():
    before = snap([], low=True)
    after = snap([rec(id="GET /a"), rec(id="GET /b")])
    result = run(before, after)
    assert result["removed"] == [] and result["changed"] == []
    assert [r["id"] for r in result["added"]] == ["GET /a", "GET /b"]
    assert all(
        r["attrs"]["low_confidence"] is True for r in result["added"]
    )
    # Still a valid, complete surface.json document.
    assert set(result) == {
        "repo", "from", "to", "generated_at",
        "added", "removed", "changed", "renamed",
    }


def test_empty_to_side_everything_removed_low_confidence():
    before = snap([rec(id="GET /a")])
    after = snap([], low=True)
    result = run(before, after)
    assert [r["id"] for r in result["removed"]] == ["GET /a"]
    assert result["removed"][0]["attrs"]["low_confidence"] is True


def test_clean_sides_do_not_get_low_confidence():
    result = run(snap([]), snap([rec(id="GET /a")]))
    assert "low_confidence" not in result["added"][0]["attrs"]


# ---- fixture pair -> golden surface.json ----


def test_fixture_semantics():
    result = run(load_fixture_side("from"), load_fixture_side("to"))
    assert [r["id"] for r in result["added"]] == ["--metrics"]
    assert [r["id"] for r in result["removed"]] == ["GET /users/{id}"]
    assert len(result["changed"]) == 1
    changed = result["changed"][0]
    assert changed["before"]["id"] == "POST /orders"
    assert "+    if len(req.items) > MAX_ITEMS:" in changed["body_diff"]
    assert len(result["renamed"]) == 1
    pair = result["renamed"][0]
    assert pair["before"]["id"] == "GET /legacy"
    assert pair["after"]["id"] == "GET /v2/legacy"


def test_fixture_golden_surface_json():
    result = run(load_fixture_side("from"), load_fixture_side("to"))
    expected = (FIXTURES / "golden_surface.json").read_text()
    assert diff.surface_json_text(result) == expected


def test_deterministic_under_input_shuffle():
    before = load_fixture_side("from")
    after = load_fixture_side("to")
    baseline = diff.surface_json_text(run(before, after))
    rng = random.Random(7)
    for _ in range(5):
        b = load_fixture_side("from")
        a = load_fixture_side("to")
        rng.shuffle(b.records)
        rng.shuffle(b.symbols)
        rng.shuffle(a.records)
        rng.shuffle(a.symbols)
        assert diff.surface_json_text(run(b, a)) == baseline


def test_write_surface_json(tmp_path):
    result = run(load_fixture_side("from"), load_fixture_side("to"))
    out = diff.write_surface_json(result, tmp_path / "v1.0.0..v1.1.0" / "surface.json")
    assert json.loads(out.read_text()) == result
    assert out.read_text() == diff.surface_json_text(result)
