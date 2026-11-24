"""End-to-end tests through the HTTP layer (FastAPI TestClient)."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import service
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    monkeypatch.setenv("ARCHIVE_DB", str(db_path))
    with TestClient(app) as c:
        c.db_path = db_path  # type: ignore[attr-defined]
        yield c


def _ndt(c, no, weld="W1", at="2026-01-01T00:00:00Z", start=0, end=1000):
    r = c.post(
        "/ndt-orders",
        json={
            "order_no": no,
            "weld_no": weld,
            "completed_at": at,
            "start_mm": start,
            "end_mm": end,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _cert(c, no, weld="W1", at="2026-01-10T00:00:00Z", start=0, end=1000, cites=()):
    r = c.post(
        "/certificates",
        json={
            "cert_no": no,
            "weld_no": weld,
            "issued_at": at,
            "start_mm": start,
            "end_mm": end,
            "ndt_order_nos": list(cites),
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _repair(c, no, weld="W1", at="2026-01-05T00:00:00Z", start=0, end=1000):
    r = c.post(
        "/repairs",
        json={
            "order_no": no,
            "weld_no": weld,
            "occurred_at": at,
            "start_mm": start,
            "end_mm": end,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _gaps(c, no):
    r = c.get(f"/certificates/{no}/gaps")
    assert r.status_code == 200, r.text
    return r.json()


def _ivs(items):
    return [(g["start_mm"], g["end_mm"]) for g in items]


def test_fully_covered_certificate_has_no_gaps(client):
    _ndt(client, "N1", end=1000)
    _cert(client, "C1", cites=["N1"])
    report = _gaps(client, "C1")
    assert report["gaps"] == []
    assert report["gap_total_mm"] == 0
    assert report["evidence"][0]["trusted"] is True


def test_uncited_ndt_is_not_evidence(client):
    _ndt(client, "N1", end=1000)
    _cert(client, "C1", cites=[])
    assert _ivs(_gaps(client, "C1")["gaps"]) == [(0, 1000)]


def test_cited_ndt_other_weld_is_not_evidence(client):
    _ndt(client, "N1", weld="W2", end=1000)
    _cert(client, "C1", weld="W1", cites=["N1"])
    report = _gaps(client, "C1")
    assert report["evidence"][0]["trusted"] is False
    assert report["evidence"][0]["reason"] == "weld_mismatch"
    assert _ivs(report["gaps"]) == [(0, 1000)]


def test_ndt_completed_at_issuance_instant_is_not_evidence(client):
    _ndt(client, "N1", at="2026-01-10T00:00:00Z", end=1000)
    _cert(client, "C1", at="2026-01-10T00:00:00Z", cites=["N1"])
    report = _gaps(client, "C1")
    assert report["evidence"][0]["reason"] == "not_completed_before_issuance"
    assert _ivs(report["gaps"]) == [(0, 1000)]


def test_repair_after_issuance_does_not_invalidate(client):
    _ndt(client, "N1", at="2026-01-01T00:00:00Z", end=1000)
    _cert(client, "C1", at="2026-01-10T00:00:00Z", cites=["N1"])
    _repair(client, "R1", at="2026-01-10T00:00:00Z", start=300, end=400)
    assert _gaps(client, "C1")["gaps"] == []


def test_repair_at_ndt_completion_does_not_invalidate(client):
    _ndt(client, "N1", at="2026-01-01T00:00:00Z", end=1000)
    _cert(client, "C1", at="2026-01-10T00:00:00Z", cites=["N1"])
    _repair(client, "R1", at="2026-01-01T00:00:00Z", start=300, end=400)
    assert _gaps(client, "C1")["gaps"] == []


def test_backfilled_repair_creates_only_intersecting_gap(client):
    _ndt(client, "N1", at="2026-01-01T00:00:00Z", end=1000)
    _cert(client, "C1", at="2026-01-10T00:00:00Z", cites=["N1"])
    result = _repair(client, "R1", at="2026-01-05T00:00:00Z", start=300, end=400)
    assert result["action"] == "created"
    assert result["changed_count"] == 1
    change = result["changed_certificates"][0]
    assert change["cert_no"] == "C1"
    assert change["gaps_before"] == []
    assert _ivs(change["gaps_after"]) == [(300, 400)]
    assert change["gap_total_before_mm"] == 0
    assert change["gap_total_after_mm"] == 100
    assert _ivs(_gaps(client, "C1")["gaps"]) == [(300, 400)]


def test_evidence_clipped_to_approved_interval(client):
    _ndt(client, "N1", start=0, end=1000)
    _cert(client, "C1", start=100, end=900, cites=["N1"])
    _repair(client, "R1", at="2026-01-05T00:00:00Z", start=400, end=500)
    report = _gaps(client, "C1")
    assert _ivs(report["gaps"]) == [(400, 500)]
    # The evidence's own coverage is still reported full, but effective is clipped.
    assert report["evidence"][0]["effective_coverage"] == [
        {"start_mm": 100, "end_mm": 400},
        {"start_mm": 500, "end_mm": 900},
    ]


def test_repair_outside_approved_interval_does_not_touch_gap(client):
    _ndt(client, "N1", start=0, end=1000)
    _cert(client, "C1", start=100, end=900, cites=["N1"])
    result = _repair(client, "R1", at="2026-01-05T00:00:00Z", start=0, end=100)
    assert result["candidates_checked"] == 0
    assert result["changed_count"] == 0
    assert _gaps(client, "C1")["gaps"] == []


def test_partial_overlap_repair_gap_is_exact_intersection(client):
    _ndt(client, "N1", start=0, end=1000)
    _cert(client, "C1", start=100, end=900, cites=["N1"])
    _repair(client, "R1", at="2026-01-05T00:00:00Z", start=0, end=450)
    assert _ivs(_gaps(client, "C1")["gaps"]) == [(100, 450)]


def test_uncited_evidence_repair_does_not_change_gap(client):
    _ndt(client, "N1", end=1000)
    _cert(client, "C1", cites=[])
    _repair(client, "R1", at="2026-01-05T00:00:00Z", start=300, end=400)
    assert _gaps(client, "C1")["gaps"] == [{"start_mm": 0, "end_mm": 1000}]


def test_two_overlapping_repairs_union_their_gaps(client):
    _ndt(client, "N1", end=1000)
    _cert(client, "C1", cites=["N1"])
    _repair(client, "R1", at="2026-01-02T00:00:00Z", start=200, end=400)
    _repair(client, "R2", at="2026-01-03T00:00:00Z", start=350, end=500)
    report = _gaps(client, "C1")
    assert _ivs(report["gaps"]) == [(200, 500)]
    assert len(report["evidence"][0]["invalidating_repairs"]) == 2


def test_correction_shrinking_repair_restores_part_of_gap(client):
    _ndt(client, "N1", end=1000)
    _cert(client, "C1", cites=["N1"])
    _repair(client, "R1", at="2026-01-05T00:00:00Z", start=300, end=400)
    assert _ivs(_gaps(client, "C1")["gaps"]) == [(300, 400)]

    r = client.put(
        "/repairs/R1",
        json={
            "weld_no": "W1",
            "occurred_at": "2026-01-05T00:00:00Z",
            "start_mm": 300,
            "end_mm": 350,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "corrected"
    assert body["changed_count"] == 1
    change = body["changed_certificates"][0]
    assert _ivs(change["gaps_before"]) == [(300, 400)]
    assert _ivs(change["gaps_after"]) == [(300, 350)]
    assert _ivs(_gaps(client, "C1")["gaps"]) == [(300, 350)]


def test_correction_moving_time_out_of_window_restores_gap(client):
    _ndt(client, "N1", end=1000)
    _cert(client, "C1", cites=["N1"])
    _repair(client, "R1", at="2026-01-05T00:00:00Z", start=300, end=400)
    # Move the repair after issuance: it no longer invalidates.
    r = client.put(
        "/repairs/R1",
        json={
            "weld_no": "W1",
            "occurred_at": "2026-01-10T00:00:00Z",
            "start_mm": 300,
            "end_mm": 400,
        },
    )
    assert r.status_code == 200, r.text
    change = r.json()["changed_certificates"][0]
    assert _ivs(change["gaps_before"]) == [(300, 400)]
    assert change["gaps_after"] == []
    assert _gaps(client, "C1")["gaps"] == []


def test_correction_moving_off_weld_restores_original_gap(client):
    _ndt(client, "N1", end=1000)
    _cert(client, "C1", cites=["N1"])
    _cert(client, "C2", weld="W2", cites=["N1"])  # N1 is W1, so C2 has no evidence
    _repair(client, "R1", at="2026-01-05T00:00:00Z", start=300, end=400)
    # Move the repair to W2: C1's gap should vanish; C2 still unaffected because
    # its only cited NDT is on another weld.
    r = client.put(
        "/repairs/R1",
        json={
            "weld_no": "W2",
            "occurred_at": "2026-01-05T00:00:00Z",
            "start_mm": 300,
            "end_mm": 400,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["cert_no"] for x in body["changed_certificates"]] == ["C1"]
    assert body["changed_certificates"][0]["gaps_after"] == []
    assert _gaps(client, "C1")["gaps"] == []
    assert _gaps(client, "C2")["gaps"] == [{"start_mm": 0, "end_mm": 1000}]


def test_unchanged_correction_reports_nothing(client):
    _ndt(client, "N1", end=1000)
    _cert(client, "C1", cites=["N1"])
    _repair(client, "R1", at="2026-01-05T00:00:00Z", start=300, end=400)
    r = client.put(
        "/repairs/R1",
        json={
            "weld_no": "W1",
            "occurred_at": "2026-01-05T00:00:00Z",
            "start_mm": 300,
            "end_mm": 400,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "unchanged"
    assert r.json()["changed_count"] == 0


def test_correction_only_rechecks_old_or_new_candidates(client):
    _ndt(client, "N1", end=1000)
    _cert(client, "C1", start=0, end=100, cites=["N1"])
    _cert(client, "C2", start=500, end=600, cites=["N1"])
    _repair(client, "R1", at="2026-01-05T00:00:00Z", start=300, end=400)
    assert _gaps(client, "C1")["gaps"] == []
    # Move the repair to overlap only C2.  C1 is a candidate (old repair did
    # not touch it either, since old repair never intersected it) while C2 is
    # new.  Old repair intersected neither -> only C2 is checked.
    r = client.put(
        "/repairs/R1",
        json={
            "weld_no": "W1",
            "occurred_at": "2026-01-05T00:00:00Z",
            "start_mm": 550,
            "end_mm": 560,
        },
    )
    body = r.json()
    assert body["candidates_checked"] == 1
    assert [x["cert_no"] for x in body["changed_certificates"]] == ["C2"]
    assert _ivs(body["changed_certificates"][0]["gaps_after"]) == [(550, 560)]


def test_duplicate_repair_number_conflicts(client):
    _ndt(client, "N1", end=1000)
    _cert(client, "C1", cites=["N1"])
    _repair(client, "R1", at="2026-01-05T00:00:00Z", start=300, end=400)
    r = client.post(
        "/repairs",
        json={
            "order_no": "R1",
            "weld_no": "W1",
            "occurred_at": "2026-01-05T00:00:00Z",
            "start_mm": 300,
            "end_mm": 400,
        },
    )
    assert r.status_code == 409


def test_correct_missing_repair_is_404(client):
    r = client.put(
        "/repairs/NOPE",
        json={
            "weld_no": "W1",
            "occurred_at": "2026-01-05T00:00:00Z",
            "start_mm": 300,
            "end_mm": 400,
        },
    )
    assert r.status_code == 404


def test_failed_correction_leaves_archive_intact(client):
    _ndt(client, "N1", end=1000)
    _cert(client, "C1", cites=["N1"])
    _repair(client, "R1", at="2026-01-05T00:00:00Z", start=300, end=400)
    # Invalid interval is rejected by validation before any write.
    r = client.put(
        "/repairs/R1",
        json={
            "weld_no": "W1",
            "occurred_at": "2026-01-05T00:00:00Z",
            "start_mm": 400,
            "end_mm": 300,
        },
    )
    assert r.status_code == 422
    assert _ivs(_gaps(client, "C1")["gaps"]) == [(300, 400)]
    assert client.get("/repairs").json()[0]["interval"] == {"start_mm": 300, "end_mm": 400}


def test_mid_transaction_failure_rolls_back_repair_and_audit(client, monkeypatch):
    _ndt(client, "N1", end=1000)
    _cert(client, "C1", cites=["N1"])

    def boom(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(service, "_audit", boom)
    with TestClient(app, raise_server_exceptions=False) as raw:
        raw.db_path = client.db_path  # type: ignore[attr-defined]
        r = raw.post(
            "/repairs",
            json={
                "order_no": "R1",
                "weld_no": "W1",
                "occurred_at": "2026-01-05T00:00:00Z",
                "start_mm": 300,
                "end_mm": 400,
            },
        )
    assert r.status_code == 500
    assert client.get("/repairs").json() == []
    with sqlite3.connect(client.db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM repair_ingest_audit").fetchone()[0]
    assert n == 0
    assert _gaps(client, "C1")["gaps"] == []


def test_unknown_citation_rejected_atomically(client):
    r = client.post(
        "/certificates",
        json={
            "cert_no": "C1",
            "weld_no": "W1",
            "issued_at": "2026-01-10T00:00:00Z",
            "start_mm": 0,
            "end_mm": 1000,
            "ndt_order_nos": ["GHOST"],
        },
    )
    assert r.status_code == 422
    assert client.get("/certificates").json() == []


def test_unknown_certificate_gap_is_404(client):
    assert client.get("/certificates/NOPE/gaps").status_code == 404


def test_swagger_and_openapi_available(client):
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "/certificates/{cert_no}/gaps" in spec["paths"]
    assert "/repairs" in spec["paths"]
    assert "put" in spec["paths"]["/repairs/{repair_no}"]


def test_repair_at_issuance_boundary_does_not_cut_but_adjacent_does(client):
    _ndt(client, "N1", end=1000)
    _cert(client, "C1", at="2026-01-10T00:00:00Z", cites=["N1"])
    _repair(client, "R1", at="2026-01-09T23:59:59Z", start=100, end=200)
    _repair(client, "R2", at="2026-01-10T00:00:00Z", start=300, end=400)
    assert _ivs(_gaps(client, "C1")["gaps"]) == [(100, 200)]


def test_trusted_scan_outside_approved_interval_adds_no_coverage(client):
    _ndt(client, "N1", start=2000, end=3000)
    _cert(client, "C1", start=0, end=1000, cites=["N1"])
    report = _gaps(client, "C1")
    ev = report["evidence"][0]
    assert ev["trusted"] is True
    assert ev["reason"] == "outside_approved_interval"
    assert ev["effective_coverage"] == []
    assert _ivs(report["gaps"]) == [(0, 1000)]
    # A repair cannot be an invalidating one for evidence that never covered
    # the approved range.
    result = _repair(client, "R1", at="2026-01-05T00:00:00Z", start=2100, end=2200)
    assert result["candidates_checked"] == 0


def test_duplicate_ndt_number_conflicts(client):
    _ndt(client, "N1")
    r = client.post(
        "/ndt-orders",
        json={
            "order_no": "N1",
            "weld_no": "W1",
            "completed_at": "2026-01-01T00:00:00Z",
            "start_mm": 0,
            "end_mm": 10,
        },
    )
    assert r.status_code == 409
    assert client.get("/certificates").json() == []


def test_healthz_reports_archive(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_duplicate_certificate_conflicts(client):
    _ndt(client, "N1")
    _cert(client, "C1", cites=["N1"])
    r = client.post(
        "/certificates",
        json={
            "cert_no": "C1",
            "weld_no": "W1",
            "issued_at": "2026-01-10T00:00:00Z",
            "start_mm": 0,
            "end_mm": 1000,
            "ndt_order_nos": ["N1"],
        },
    )
    assert r.status_code == 409


def test_invalid_intervals_rejected_for_ndt_and_repairs(client):
    r = client.post(
        "/ndt-orders",
        json={
            "order_no": "N1",
            "weld_no": "W1",
            "completed_at": "2026-01-01T00:00:00Z",
            "start_mm": 10,
            "end_mm": 10,
        },
    )
    assert r.status_code == 422
    r = client.post(
        "/repairs",
        json={
            "order_no": "R1",
            "weld_no": "W1",
            "occurred_at": "2026-01-05T00:00:00Z",
            "start_mm": 0,
            "end_mm": 0,
        },
    )
    assert r.status_code == 422
