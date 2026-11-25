"""Read-only reverse lookup: solve a repair's time from recorded gaps."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import service
from app.db import connect, init_db
from app.intervals import Interval
from app.main import app
from app.models import (
    CertificateGapsIn,
    CertificateIn,
    GapIntervalIn,
    NdtOrderIn,
    RepairIn,
    RepairLookupRequest,
)


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


def _cert(c, no, weld="W1", at="2026-01-10T00:00:00Z", start=0, end=1000, cites=("N1",)):
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


def _repair(c, no, weld="W1", at="2026-01-05T00:00:00Z", start=300, end=400):
    r = c.post(
        "/repairs",
        json={"order_no": no, "weld_no": weld, "occurred_at": at, "start_mm": start, "end_mm": end},
    )
    assert r.status_code == 201, r.text


def _lookup(c, repair_no="R1", a="2026-01-01T00:00:00Z", b="2026-01-10T00:00:00Z", certs=()):
    r = c.post(
        f"/repairs/{repair_no}/time-lookup",
        json={
            "window_start": a,
            "window_end": b,
            "certificates": [{"cert_no": no, "gaps": list(gaps)} for no, gaps in certs],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _spans(solutions):
    return [
        (s["start"], s["end"], s["start_inclusive"], s["end_inclusive"])
        for s in solutions["intervals"]
    ]


def test_single_instant_answer_is_listed_as_instant(client):
    _ndt(client, "N1")
    _cert(client, "C1")
    _repair(client, "R1")
    body = _lookup(client, certs=[("C1", [])])
    assert body["status"] == "found"
    # The moved repair must sit strictly between film and issuance: only the
    # window endpoints match here, and they are isolated instants.
    assert body["common"]["intervals"] == []
    assert body["common"]["instants"] == [
        "2026-01-01T00:00:00.000000Z",
        "2026-01-10T00:00:00.000000Z",
    ]


def test_span_is_reported_with_open_endpoints(client):
    _ndt(client, "N1")
    _cert(client, "C1")
    _repair(client, "R1")
    body = _lookup(
        client,
        a="2026-01-02T00:00:00Z",
        b="2026-01-08T00:00:00Z",
        certs=[("C1", [{"start_mm": 300, "end_mm": 400}])],
    )
    assert body["status"] == "found"
    # Every time from the film to issuance would cut 300-400; the search
    # window is the inclusive subset of that, so both borders are included.
    assert _spans(body["common"]) == [
        ("2026-01-02T00:00:00.000000Z", "2026-01-08T00:00:00.000000Z", True, True)
    ]
    assert body["common"]["instants"] == []
    assert body["certificates"][0]["has_solution"] is True


def test_mixed_gap_list_needs_split_times(client):
    # One certificate cites two films.  A repair early enough to cut the first
    # but too late for the second yields different gap shapes.
    _ndt(client, "N1", at="2026-01-01T00:00:00Z", start=0, end=500)
    _ndt(client, "N2", at="2026-01-06T00:00:00Z", start=500, end=1000)
    _cert(client, "C1", at="2026-01-10T00:00:00Z", cites=["N1", "N2"])
    _repair(client, "R1", at="2026-01-05T00:00:00Z", start=600, end=700)
    # 600–700 is covered by N2 only, so only the late window invalidates it.
    body = _lookup(
        client,
        certs=[("C1", [{"start_mm": 600, "end_mm": 700}])],
    )
    assert body["status"] == "found"
    assert _spans(body["certificates"][0]["solutions"]) == [
        ("2026-01-06T00:00:00.000000Z", "2026-01-10T00:00:00.000000Z", False, False)
    ]


def test_no_common_time_is_reported(client):
    _ndt(client, "N1", at="2026-01-01T00:00:00Z")
    _ndt(client, "N2", at="2026-01-20T00:00:00Z")
    _cert(client, "C1", at="2026-01-10T00:00:00Z", cites=["N1"])
    _cert(client, "C2", at="2026-02-01T00:00:00Z", cites=["N2"])
    _repair(client, "R1", at="2026-01-05T00:00:00Z")
    body = _lookup(
        client,
        a="2026-01-01T00:00:00Z",
        b="2026-02-01T00:00:00Z",
        certs=[
            ("C1", [{"start_mm": 300, "end_mm": 400}]),
            ("C2", [{"start_mm": 300, "end_mm": 400}]),
        ],
    )
    assert body["status"] == "no_common_time"
    assert body["common"]["intervals"] == []
    assert body["common"]["instants"] == []
    assert all(c["has_solution"] for c in body["certificates"])
    assert _spans(body["certificates"][0]["solutions"]) == [
        ("2026-01-01T00:00:00.000000Z", "2026-01-10T00:00:00.000000Z", False, False)
    ]
    assert _spans(body["certificates"][1]["solutions"]) == [
        ("2026-01-20T00:00:00.000000Z", "2026-02-01T00:00:00.000000Z", False, False)
    ]


def test_certificate_without_any_solution_is_unsatisfiable(client):
    _ndt(client, "N1")
    _cert(client, "C1")
    _repair(client, "R1")
    body = _lookup(client, certs=[("C1", [{"start_mm": 100, "end_mm": 200}])])
    assert body["status"] == "unsatisfiable"
    assert body["certificates"][0]["has_solution"] is False
    assert body["certificates"][0]["solutions"] == {"intervals": [], "instants": []}
    assert body["common"] == {"intervals": [], "instants": []}


def test_empty_gap_list_means_no_gaps(client):
    _ndt(client, "N1")
    _cert(client, "C1")
    _repair(client, "R1")
    # The whole search window lies strictly between film and issuance, so the
    # repair always cuts 300-400 there: an empty expected list has no match.
    body = _lookup(client, a="2026-01-02T00:00:00Z", b="2026-01-08T00:00:00Z", certs=[("C1", [])])
    assert body["certificates"][0]["expected_gaps"] == []
    assert body["status"] == "unsatisfiable"
    assert body["certificates"][0]["has_solution"] is False

    # Widen to the true window: only the film and issuance instants themselves
    # leave the repair ineffective, so those two isolated instants match.
    body = _lookup(client, certs=[("C1", [])])
    assert body["status"] == "found"
    assert body["common"]["instants"] == [
        "2026-01-01T00:00:00.000000Z",
        "2026-01-10T00:00:00.000000Z",
    ]


def test_overlapping_expected_gaps_are_normalised(client):
    _ndt(client, "N1")
    _cert(client, "C1")
    _repair(client, "R1")
    body = _lookup(
        client,
        a="2026-01-02T00:00:00Z",
        b="2026-01-04T00:00:00Z",
        certs=[("C1", [{"start_mm": 300, "end_mm": 400}, {"start_mm": 300, "end_mm": 400}])],
    )
    assert body["certificates"][0]["expected_gaps"] == [{"start_mm": 300, "end_mm": 400}]
    assert body["status"] == "found"


def test_lookup_does_not_write_and_ignores_later_changes(client):
    _ndt(client, "N1")
    _cert(client, "C1")
    _repair(client, "R1")
    before_repairs = client.get("/repairs").json()
    body = _lookup(client, certs=[("C1", [{"start_mm": 300, "end_mm": 400}])])
    assert body["status"] == "found"
    assert client.get("/repairs").json() == before_repairs
    with connect(client.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM repair_ingest_audit").fetchone()[0] == 1
    # A later correction changes only the write path; the lookup stays read-only.
    r = client.put(
        "/repairs/R1",
        json={
            "weld_no": "W1",
            "occurred_at": "2026-01-06T00:00:00Z",
            "start_mm": 300,
            "end_mm": 400,
        },
    )
    assert r.status_code == 200
    again = _lookup(client, certs=[("C1", [{"start_mm": 300, "end_mm": 400}])])
    assert again["repair"]["recorded_occurred_at"] == "2026-01-06T00:00:00.000000Z"
    assert again["status"] == "found"


def test_unknown_repair_or_certificate_is_404(client):
    _ndt(client, "N1")
    _cert(client, "C1")
    _repair(client, "R1")
    assert (
        client.post(
            "/repairs/NOPE/time-lookup",
            json={
                "window_start": "2026-01-01T00:00:00Z",
                "window_end": "2026-01-02T00:00:00Z",
                "certificates": [{"cert_no": "C1", "gaps": []}],
            },
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/repairs/R1/time-lookup",
            json={
                "window_start": "2026-01-01T00:00:00Z",
                "window_end": "2026-01-02T00:00:00Z",
                "certificates": [{"cert_no": "NOPE", "gaps": []}],
            },
        ).status_code
        == 404
    )


def test_bad_requests_are_rejected(client):
    _ndt(client, "N1")
    _cert(client, "C1")
    _repair(client, "R1")
    base = {"window_start": "2026-01-01T00:00:00Z", "window_end": "2026-01-02T00:00:00Z"}
    empty = client.post("/repairs/R1/time-lookup", json={**base, "certificates": []})
    assert empty.status_code == 422
    assert (
        client.post(
            "/repairs/R1/time-lookup",
            json={
                **base,
                "window_start": "2026-01-03T00:00:00Z",
                "certificates": [{"cert_no": "C1", "gaps": []}],
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/repairs/R1/time-lookup",
            json={
                **base,
                "certificates": [{"cert_no": "C1", "gaps": [{"start_mm": 10, "end_mm": 10}]}],
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/repairs/R1/time-lookup",
            json={
                **base,
                "certificates": [{"cert_no": "C1", "gaps": []}, {"cert_no": "C1", "gaps": []}],
            },
        ).status_code
        == 422
    )


def test_documented_in_openapi(client):
    spec = client.get("/openapi.json").json()
    assert "/repairs/{repair_no}/time-lookup" in spec["paths"]
    assert "post" in spec["paths"]["/repairs/{repair_no}/time-lookup"]


# --------------------------------------------------------------------------
# Equivalence against brute-force sampling
# --------------------------------------------------------------------------


def _truth_gaps(conn, cert_no, probe, at):
    return service._load_cert(conn, cert_no, probe=probe, at=at).gaps


def _contains(solutions, moment):
    for span in solutions["intervals"]:
        lo = datetime.strptime(span["start"], service.CANONICAL_TS).replace(tzinfo=UTC)
        hi = datetime.strptime(span["end"], service.CANONICAL_TS).replace(tzinfo=UTC)
        if lo < moment < hi:
            return True
        if moment == lo and span["start_inclusive"]:
            return True
        if moment == hi and span["end_inclusive"]:
            return True
    return any(
        datetime.strptime(x, service.CANONICAL_TS).replace(tzinfo=UTC) == moment
        for x in solutions["instants"]
    )


def test_random_lookups_match_brute_force_sampling(tmp_path):
    rng = random.Random(20261125)
    db_path = tmp_path / "archive.db"
    init_db(db_path)
    conn = connect(db_path)
    base = datetime(2026, 1, 1, tzinfo=UTC)

    def day(n: int) -> datetime:
        return base + timedelta(days=n)

    for trial in range(40):
        # Fresh archive each round so ids do not accumulate across cases.
        conn.close()
        db_path = tmp_path / f"archive-{trial}.db"
        init_db(db_path)
        conn = connect(db_path)
        welds = ["W1", "W2"]
        created_ndts = 0
        for i in range(rng.randint(1, 3)):
            weld = rng.choice(welds)
            start = rng.randrange(0, 600, 50)
            service.create_ndt(
                conn,
                NdtOrderIn(
                    order_no=f"N{i}",
                    weld_no=weld,
                    completed_at=day(rng.randint(0, 5)),
                    start_mm=start,
                    end_mm=start + rng.randrange(50, 400, 50),
                ),
            )
            created_ndts += 1
        certs = []
        for j in range(rng.randint(1, 3)):
            weld = rng.choice(welds)
            start = rng.randrange(0, 600, 50)
            service.create_certificate(
                conn,
                CertificateIn(
                    cert_no=f"C{j}",
                    weld_no=weld,
                    issued_at=day(rng.randint(8, 15)),
                    start_mm=start,
                    end_mm=start + rng.randrange(50, 500, 50),
                    ndt_order_nos=[f"N{i}" for i in range(rng.randint(0, 3)) if i < created_ndts],
                ),
            )
            certs.append(f"C{j}")
        r_start = rng.randrange(0, 800, 50)
        r_iv = Interval(r_start, r_start + rng.randrange(50, 400, 50))
        weld = rng.choice(welds)
        service.ingest_repair(
            conn,
            RepairIn(
                order_no="R1",
                weld_no=weld,
                occurred_at=day(rng.randint(0, 16)),
                start_mm=r_iv.start,
                end_mm=r_iv.end,
            ),
        )
        probe = service.RepairLookupProbe("R1", weld, r_iv)
        a = day(rng.randint(0, 6))
        b = a + timedelta(days=rng.randint(0, 10))

        # Derive each certificate's expected gaps at a random true instant.
        target = day(rng.randint(0, 16)) + timedelta(minutes=rng.randint(0, 59))
        expected = {c: _truth_gaps(conn, c, probe, target) for c in certs}
        result = service.lookup_repair_time(
            conn,
            "R1",
            RepairLookupRequest(
                window_start=a,
                window_end=b,
                certificates=[
                    CertificateGapsIn(
                        cert_no=c,
                        gaps=[GapIntervalIn(start_mm=g.start, end_mm=g.end) for g in expected[c]],
                    )
                    for c in certs
                ],
            ),
        )
        out = result.model_dump()

        # Sample every boundary, every midpoint, and both neighbours.
        boundaries = {a, b}
        for c in certs:
            snap = service._load_cert(conn, c)
            boundaries.add(snap.issued_at)
            for ev in snap.evidence:
                if ev.completed_at is not None:
                    boundaries.add(ev.completed_at)
        stamps = sorted(boundaries)
        samples = set(stamps)
        for i in range(len(stamps) - 1):
            samples.add(stamps[i] + (stamps[i + 1] - stamps[i]) // 2)
        for moment in stamps:
            samples.add(moment - timedelta(microseconds=1))
            samples.add(moment + timedelta(microseconds=1))

        for moment in samples:
            if not (a <= moment <= b):
                continue
            want_cert = {c: _truth_gaps(conn, c, probe, moment) == expected[c] for c in certs}
            for item in out["certificates"]:
                assert _contains(item["solutions"], moment) == want_cert[item["cert_no"]], (
                    trial,
                    item["cert_no"],
                    moment,
                )
            assert _contains(out["common"], moment) == all(want_cert.values()), (trial, moment)

        for item in out["certificates"]:
            assert item["has_solution"] == bool(
                item["solutions"]["intervals"] or item["solutions"]["instants"]
            )
        if a <= target <= b:
            assert out["status"] == "found"
        if any(not item["has_solution"] for item in out["certificates"]):
            assert out["status"] == "unsatisfiable"
        elif out["status"] != "found":
            assert out["status"] == "no_common_time"

    conn.close()


def test_lookup_reads_one_committed_snapshot_while_writes_continue(client, monkeypatch):
    """A correction committed mid-lookup must not leak into the answer."""
    _ndt(client, "N1", at="2026-01-01T00:00:00Z")
    _cert(client, "C1", cites=["N1"])
    _cert(client, "C2", cites=["N1"])
    _repair(client, "R1", at="2026-01-05T00:00:00Z", start=300, end=400)

    original = service._load_cert
    seen: list[str] = []

    def interleaved(conn, cert_no, **kwargs):
        seen.append(cert_no)
        if len(seen) == 2:
            # Once both certificates are loaded, another connection corrects
            # the repair; the lookup's snapshot is already established.  WAL
            # lets that write commit while this read transaction is open.
            other = connect(client.db_path)
            try:
                service.correct_repair(
                    other,
                    "R1",
                    service.RepairUpdateIn(
                        weld_no="W1",
                        occurred_at=datetime(2026, 1, 7, tzinfo=UTC),
                        start_mm=300,
                        end_mm=400,
                    ),
                )
            finally:
                other.close()
        return original(conn, cert_no, **kwargs)

    monkeypatch.setattr(service, "_load_cert", interleaved)
    body = _lookup(
        client,
        a="2026-01-02T00:00:00Z",
        b="2026-01-08T00:00:00Z",
        certs=[
            ("C1", [{"start_mm": 300, "end_mm": 400}]),
            ("C2", [{"start_mm": 300, "end_mm": 400}]),
        ],
    )
    assert set(seen) == {"C1", "C2"}
    # The lookup saw the pre-correction committed state from start to finish.
    assert body["repair"]["recorded_occurred_at"] == "2026-01-05T00:00:00.000000Z"
    # The write did commit and is visible to later readers.
    assert client.get("/repairs").json()[0]["occurred_at"] == "2026-01-07T00:00:00.000000Z"


def test_repair_on_other_weld_never_affects_the_certificate(client):
    """A cross-weld repair is not evidence for this certificate at any time."""
    _ndt(client, "N1", weld="W1", at="2026-01-01T00:00:00Z", start=0, end=1000)
    _cert(client, "C1", weld="W1", cites=["N1"])
    _repair(client, "R1", weld="W2", at="2026-01-05T00:00:00Z", start=300, end=400)
    body = _lookup(
        client,
        a="2026-01-02T00:00:00Z",
        b="2026-01-08T00:00:00Z",
        certs=[("C1", [])],
    )
    assert body["status"] == "found"
    # The whole window is a solution: moving the other-weld repair's time
    # changes nothing for this certificate.
    assert _spans(body["common"]) == [
        ("2026-01-02T00:00:00.000000Z", "2026-01-08T00:00:00.000000Z", True, True)
    ]
    assert body["common"]["instants"] == []
