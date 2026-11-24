"""Property test: incremental repair ingest must match a full recomputation.

For every random archive and every operation, the set of certificates reported
as changed must equal the set whose gap actually moved, and each reported
before/after pair must equal a brute-force recomputation.  Candidates checked
must equal exactly the certificates that the old/new repair *could* truncate
under the documented rule.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from app import service
from app.db import connect, init_db
from app.intervals import Interval
from app.models import CertificateIn, NdtOrderIn, RepairIn, RepairUpdateIn

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _dt(day: int) -> datetime:
    return BASE + timedelta(days=day)


def _gaps_all(conn, cert_nos):
    return {no: service._load_cert(conn, no).gaps for no in cert_nos}


def _expected_candidates(conn, probes):
    """Independent restatement of the candidate rule."""
    certs = [r["cert_no"] for r in conn.execute("SELECT cert_no FROM certificates")]
    out = set()
    for cert_no in certs:
        snap = service._load_cert(conn, cert_no)
        for probe in probes:
            if probe.weld_no != snap.weld_no:
                continue
            for ev in snap.evidence:
                if not ev.trusted or ev.completed_at is None:
                    continue
                if not (ev.completed_at < probe.occurred_at < snap.issued_at):
                    continue
                clipped = ev.interval.intersection(snap.approved)
                if clipped is not None and clipped.intersects(probe.interval):
                    out.add(cert_no)
    return out


def test_random_ingest_and_corrections_match_full_recompute(tmp_path):
    rng = random.Random(20261124)
    db_path = tmp_path / "archive.db"
    init_db(db_path)
    conn = connect(db_path)

    welds = ["W1", "W2", "W3"]
    ndt_n = 0
    cert_n = 0
    repair_n = 0
    cert_nos: list[str] = []
    repair_specs: list[dict] = []

    def make_ndt():
        nonlocal ndt_n
        ndt_n += 1
        no = f"N{ndt_n}"
        weld = rng.choice(welds)
        day = rng.randint(0, 20)
        start = rng.randrange(0, 700, 50)
        end = start + rng.randrange(50, 600, 50)
        service.create_ndt(
            conn,
            NdtOrderIn(
                order_no=no,
                weld_no=weld,
                completed_at=_dt(day),
                start_mm=start,
                end_mm=end,
            ),
        )
        return no

    def make_cert(ndt_pool):
        nonlocal cert_n
        cert_n += 1
        no = f"C{cert_n}"
        weld = rng.choice(welds)
        day = rng.randint(5, 30)
        start = rng.randrange(0, 700, 50)
        end = start + rng.randrange(50, 600, 50)
        cites = rng.sample(ndt_pool, k=min(len(ndt_pool), rng.randint(0, 3)))
        service.create_certificate(
            conn,
            CertificateIn(
                cert_no=no,
                weld_no=weld,
                issued_at=_dt(day),
                start_mm=start,
                end_mm=end,
                ndt_order_nos=cites,
            ),
        )
        cert_nos.append(no)

    def op_ingest():
        nonlocal repair_n
        repair_n += 1
        no = f"R{repair_n}"
        weld = rng.choice(welds)
        day = rng.randint(0, 32)
        start = rng.randrange(0, 800, 50)
        end = start + rng.randrange(50, 400, 50)
        spec = {
            "order_no": no,
            "weld_no": weld,
            "occurred_at": _dt(day),
            "start_mm": start,
            "end_mm": end,
        }
        probe = service.RepairProbe(weld, _dt(day), Interval(start, end))
        expected_candidates = _expected_candidates(conn, [probe])
        before = _gaps_all(conn, cert_nos)
        result = service.ingest_repair(conn, RepairIn(**spec))
        repair_specs.append(spec)
        after = _gaps_all(conn, cert_nos)
        actual_changed = {c for c in cert_nos if before[c] != after[c]}
        reported_changed = {c.cert_no for c in result.changed_certificates}
        assert reported_changed == actual_changed, (no, reported_changed, actual_changed)
        assert result.candidates_checked == len(expected_candidates)
        assert actual_changed <= expected_candidates
        by_no = {c.cert_no: c for c in result.changed_certificates}
        for cert_no in actual_changed:
            got = by_no[cert_no]
            assert [Interval(g.start_mm, g.end_mm) for g in got.gaps_before] == before[cert_no]
            assert [Interval(g.start_mm, g.end_mm) for g in got.gaps_after] == after[cert_no]

    def op_correct():
        if not repair_specs:
            return
        idx = rng.randrange(len(repair_specs))
        no = repair_specs[idx]["order_no"]
        row = conn.execute(
            "SELECT weld_no, occurred_at, start_mm, end_mm FROM repairs WHERE order_no = ?",
            (no,),
        ).fetchone()
        old_probe = service.RepairProbe(
            row["weld_no"],
            service.from_ts(row["occurred_at"]),
            Interval(row["start_mm"], row["end_mm"]),
        )
        weld = rng.choice(welds)
        day = rng.randint(0, 32)
        start = rng.randrange(0, 800, 50)
        end = start + rng.randrange(50, 400, 50)
        new_probe = service.RepairProbe(weld, _dt(day), Interval(start, end))
        expected_candidates = _expected_candidates(conn, [old_probe, new_probe])
        before = _gaps_all(conn, cert_nos)
        result = service.correct_repair(
            conn,
            no,
            RepairUpdateIn(
                weld_no=weld,
                occurred_at=_dt(day),
                start_mm=start,
                end_mm=end,
            ),
        )
        repair_specs[idx] = {
            "order_no": no,
            "weld_no": weld,
            "occurred_at": _dt(day),
            "start_mm": start,
            "end_mm": end,
        }
        after = _gaps_all(conn, cert_nos)
        actual_changed = {c for c in cert_nos if before[c] != after[c]}
        reported_changed = {c.cert_no for c in result.changed_certificates}
        assert reported_changed == actual_changed, (no, reported_changed, actual_changed)
        assert result.candidates_checked == len(expected_candidates)
        assert actual_changed <= expected_candidates
        by_no = {c.cert_no: c for c in result.changed_certificates}
        for cert_no in actual_changed:
            got = by_no[cert_no]
            assert [Interval(g.start_mm, g.end_mm) for g in got.gaps_before] == before[cert_no]
            assert [Interval(g.start_mm, g.end_mm) for g in got.gaps_after] == after[cert_no]

    ndt_pool: list[str] = []
    for _ in range(4):
        ndt_pool.append(make_ndt())
    for _ in range(6):
        make_cert(ndt_pool)
    for _ in range(6):
        ndt_pool.append(make_ndt())
    for _ in range(4):
        make_cert(ndt_pool)

    for _ in range(120):
        if rng.random() < 0.55:
            op_ingest()
        else:
            op_correct()

    conn.close()


def test_concurrent_ingests_serialize_without_lost_updates(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    db_path = tmp_path / "archive.db"
    init_db(db_path)
    setup = connect(db_path)
    service.create_ndt(
        setup,
        NdtOrderIn(
            order_no="N1",
            weld_no="W1",
            completed_at=_dt(0),
            start_mm=0,
            end_mm=1000,
        ),
    )
    for i in range(6):
        service.create_certificate(
            setup,
            CertificateIn(
                cert_no=f"C{i}",
                weld_no="W1",
                issued_at=_dt(10),
                start_mm=0,
                end_mm=1000,
                ndt_order_nos=["N1"],
            ),
        )
    setup.close()

    def worker(i: int):
        conn = connect(db_path)
        try:
            return service.ingest_repair(
                conn,
                RepairIn(
                    order_no=f"R{i}",
                    weld_no="W1",
                    occurred_at=_dt(5),
                    start_mm=i * 100,
                    end_mm=i * 100 + 50,
                ),
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM repairs").fetchone()[0] == 8
    assert conn.execute("SELECT COUNT(*) FROM repair_ingest_audit").fetchone()[0] == 8
    assert sorted(r.repair.order_no for r in results) == [f"R{i}" for i in range(8)]
    # The gap is what the repairs removed, not what remains covered.
    expected = sorted(Interval(i * 100, i * 100 + 50) for i in range(8))
    for i in range(6):
        assert service._load_cert(conn, f"C{i}").gaps == expected
    conn.close()


def test_no_probes_yields_no_candidates(tmp_path):
    db_path = tmp_path / "archive.db"
    init_db(db_path)
    conn = connect(db_path)
    assert service._candidate_certs(conn, []) == {}
    conn.close()
