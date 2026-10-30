"""Ranking: the spec's scoring formula over the combined delta (R14, KTD8).

    score = surface delta weight + guard hunk weight + reach bonus

Surface delta weight is the weight of the highest-weighted delta record
sharing the hunk's enclosing symbol: a hunk inside a function that is
itself a surface delta ranks first. Guard weight is the hunk's direction
weight (removed 3, added 2, modified 1; see guards.DIRECTION_WEIGHT). The
reach bonus applies when the hunk's symbol is reachable from a new or
changed entry point through the helpers' same-package call edges (KTD8),
not name matching.

Two magnitudes the spec leaves unspecified:

- `outbound` records get weight 1, like `export`: the spec enumerates
  route/rpc/protocol 3, flag/config 2, export 1, and a new egress
  destination is the mildest remaining surface signal.
- REACH_BONUS is 2: a reachable added or modified guard outranks an
  unreachable removed one only when the delta-symbol weight does not
  already separate them.

Ordering is deterministic: score descending, then the hunk's own
coordinates; delta records order by descending weight with the plan's
(kind, id) tie-break (R16).
"""

from dataclasses import dataclass

from .guards import GuardHunk
from .records import CallEdge

SURFACE_WEIGHT = {
    "route": 3,
    "rpc": 3,
    "protocol_id": 3,
    "cli_flag": 2,
    "config_key": 2,
    "export": 1,
    "outbound": 1,
}

# Spec: "+ reach bonus" with no number. See module docstring for the choice.
REACH_BONUS = 2


def surface_weight(kind: str) -> int:
    """The spec's per-kind delta weight; unknown kinds score 0."""
    return SURFACE_WEIGHT.get(kind, 0)


def iter_delta_records(surface: dict):
    """Yield every record dict touched by the delta, both sides included.

    Order: added, removed, changed (before then after), renamed (before
    then after). Used for symbol-weight attribution and low-confidence
    counting; the caller sorts for display.
    """
    yield from surface.get("added", [])
    yield from surface.get("removed", [])
    for pair in surface.get("changed", []):
        yield pair.get("before") or {}
        yield pair.get("after") or {}
    for pair in surface.get("renamed", []):
        yield pair.get("before") or {}
        yield pair.get("after") or {}


def _symbol_weights(surface: dict) -> dict[str, int]:
    """symbol -> max surface weight over every delta record.

    Both sides count: a removed function is a surface delta too, and its
    hunks rank by the same rule.
    """
    weights: dict[str, int] = {}
    for rec in iter_delta_records(surface):
        sym = rec.get("symbol") or ""
        if not sym:
            continue
        w = surface_weight(rec.get("kind", ""))
        if w > weights.get(sym, 0):
            weights[sym] = w
    return weights


def _entry_symbols(surface: dict) -> set[str]:
    """Symbols of new or changed entry points: the reach-bonus roots.

    Added records plus the after-side of changed and renamed pairs.
    """
    roots = set()
    for rec in surface.get("added", []):
        if rec.get("symbol"):
            roots.add(rec["symbol"])
    for pair in surface.get("changed", []):
        sym = (pair.get("after") or {}).get("symbol")
        if sym:
            roots.add(sym)
    for pair in surface.get("renamed", []):
        sym = (pair.get("after") or {}).get("symbol")
        if sym:
            roots.add(sym)
    return roots


def reachable_symbols(edges, roots) -> set[str]:
    """Symbols transitively callable from `roots` (roots included).

    Edges are (caller, callee) pairs resolvable within one package/module
    (KTD8); plain symbol-name BFS is the cheap graph walk the spec asks
    for. Roots count as reachable so a hunk inside an entry point that is
    also a surface delta gets both terms.
    """
    callees: dict[str, list[str]] = {}
    for e in edges or ():
        if e.caller and e.callee:
            callees.setdefault(e.caller, []).append(e.callee)
    seen = set(roots)
    stack = list(roots)
    while stack:
        for callee in callees.get(stack.pop(), ()):
            if callee not in seen:
                seen.add(callee)
                stack.append(callee)
    return seen


@dataclass(frozen=True)
class RankedHunk:
    """A guard hunk with its score split into the formula's three terms."""

    hunk: GuardHunk
    surface_weight: int
    guard_weight: int
    reach_bonus: int

    @property
    def score(self) -> int:
        return self.surface_weight + self.guard_weight + self.reach_bonus

    @property
    def in_delta_symbol(self) -> bool:
        """Hunk sits inside a function that is itself a surface delta."""
        return self.surface_weight > 0

    @property
    def reachable(self) -> bool:
        return self.reach_bonus > 0


def _hunk_order(r: RankedHunk) -> tuple:
    h = r.hunk
    return (
        -r.score,
        h.file,
        h.new_start,
        h.new_count,
        h.old_start,
        h.old_count,
        h.hunk_class,
        h.direction,
    )


def rank_hunks(hunks, surface: dict, edges=()) -> list[RankedHunk]:
    """Score every guard hunk and return them highest-risk first (R16)."""
    sym_weights = _symbol_weights(surface)
    reach = reachable_symbols(edges, _entry_symbols(surface))
    ranked = []
    for h in hunks:
        sym = h.symbol or ""
        ranked.append(
            RankedHunk(
                hunk=h,
                surface_weight=sym_weights.get(sym, 0),
                guard_weight=h.weight,
                reach_bonus=REACH_BONUS if sym and sym in reach else 0,
            )
        )
    ranked.sort(key=_hunk_order)
    return ranked


def _record_order(rec: dict) -> tuple:
    """Descending weight, then the plan's (kind, id) tie-break."""
    rec = rec or {}
    return (
        -surface_weight(rec.get("kind", "")),
        rec.get("kind", ""),
        rec.get("id", ""),
        rec.get("file", ""),
        int(rec.get("line") or 0),
        rec.get("symbol", ""),
    )


def rank_delta(surface: dict) -> dict[str, list]:
    """The four delta lists, each sorted highest-weight first (R16).

    changed/renamed entries stay {before, after} pairs ordered by the
    after-side record.
    """
    def pair_key(pair: dict) -> tuple:
        return _record_order(pair.get("after") or pair.get("before") or {})

    return {
        "added": sorted(surface.get("added", []), key=_record_order),
        "removed": sorted(surface.get("removed", []), key=_record_order),
        "changed": sorted(surface.get("changed", []), key=pair_key),
        "renamed": sorted(surface.get("renamed", []), key=pair_key),
    }
