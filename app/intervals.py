"""Half-open interval arithmetic, in millimetres.

Every interval in this service is ``[start, end)``: the start is included and
the end is excluded.  A zero-length interval is never representable; such
inputs are rejected by the API layer.  All values are integer millimetres so
that intersection/complement results are exact.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Interval:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"empty interval [{self.start}, {self.end})")

    def intersects(self, other: Interval) -> bool:
        # Half-open: [0,10) and [10,20) do not intersect.
        return self.start < other.end and other.start < self.end

    def intersection(self, other: Interval) -> Interval | None:
        lo = max(self.start, other.start)
        hi = min(self.end, other.end)
        if hi <= lo:
            return None
        return Interval(lo, hi)


def subtract(base: Interval, cut: Interval) -> list[Interval]:
    """Return ``base`` minus ``cut`` as a sorted list of intervals."""
    if not base.intersects(cut):
        return [base]
    pieces: list[Interval] = []
    if base.start < cut.start:
        pieces.append(Interval(base.start, min(cut.start, base.end)))
    if cut.end < base.end:
        pieces.append(Interval(max(cut.end, base.start), base.end))
    return [p for p in pieces if p.end > p.start]


def subtract_all(bases: list[Interval], cuts: list[Interval]) -> list[Interval]:
    """Subtract every interval in ``cuts`` from every interval in ``bases``."""
    result = list(bases)
    for cut in cuts:
        nxt: list[Interval] = []
        for piece in result:
            nxt.extend(subtract(piece, cut))
        result = nxt
    return sorted(set(result))
