from __future__ import annotations

import pytest

from app.intervals import Interval, subtract, subtract_all


def test_half_open_adjacency_does_not_intersect():
    assert not Interval(0, 10).intersects(Interval(10, 20))
    assert Interval(0, 10).intersection(Interval(10, 20)) is None


def test_half_open_overlap_by_one_mm():
    assert Interval(0, 10).intersects(Interval(9, 20))
    assert Interval(0, 10).intersection(Interval(9, 20)) == Interval(9, 10)


def test_subtract_interior_creates_two_pieces():
    assert subtract(Interval(0, 100), Interval(40, 60)) == [Interval(0, 40), Interval(60, 100)]


def test_subtract_touching_edge_is_noop():
    assert subtract(Interval(0, 10), Interval(10, 20)) == [Interval(0, 10)]


def test_subtract_covering_removes_all():
    assert subtract(Interval(10, 20), Interval(0, 20)) == []


def test_subtract_multiple_cuts():
    pieces = subtract_all([Interval(0, 100)], [Interval(10, 20), Interval(20, 30)])
    assert pieces == [Interval(0, 10), Interval(30, 100)]


def test_empty_interval_rejected():
    with pytest.raises(ValueError):
        Interval(5, 5)
