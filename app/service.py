"""Core rules for gap checks and repair ingest.

Evidence rules (per certificate):

* Only NDT orders **cited by the certificate** are considered.
* A cited order must be for the **same weld**.
* It must be **completed before the certificate is issued**.  An order
  completed at exactly the issuance instant cannot have backed the
  certificate and is not usable.
* Its coverage is clipped to the certificate's approved interval; portions of
  a scan outside the approved interval never fill a gap.
* A repair invalidates the intersection of its interval with that clipped
  coverage only when the repair happens **after the NDT order completed and
  before the certificate was issued**.  Repairs at exactly either boundary do
  not invalidate; repairs at/after issuance cannot retroactively remove the
  basis the certificate was signed on.

Certificate gap = approved interval minus the union of trusted, non-invalidated
evidence coverage.

Repair ingest re-checks only certificates whose cited trusted evidence the
*old* repair or the *new* repair can truncate.  "Can truncate" is exact: same
weld, repair time strictly between the evidence completion and certificate
issuance, and repair interval intersecting the evidence coverage clipped to
the approved interval.  A repair that cannot meet all three leaves every gap
untouched and is never recomputed or reported as changed.

The repair row (insert or update) and its audit record (which carries the
before/after gaps) are written in one ``BEGIN IMMEDIATE`` transaction.  Any
failure rolls back the whole unit, leaving the pre-existing archive intact and
queryable.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from .db import read_tx, write_tx
from .intervals import Interval, subtract_all
from .models import (
    CertificateIn,
    CertificateOut,
    ChangedCertificate,
    EvidenceOut,
    GapReport,
    IntervalOut,
    NdtOrderIn,
    NdtOrderOut,
    RepairIn,
    RepairIngestResult,
    RepairOut,
    RepairRef,
    RepairUpdateIn,
)

CANONICAL_TS = "%Y-%m-%dT%H:%M:%S.%fZ"


def to_ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime(CANONICAL_TS)


def from_ts(value: str) -> datetime:
    return datetime.strptime(value, CANONICAL_TS).replace(tzinfo=UTC)


def _iv(start: int, end: int) -> Interval:
    return Interval(int(start), int(end))


def _iv_out(value: Interval) -> IntervalOut:
    return IntervalOut(start_mm=value.start, end_mm=value.end)


def _iv_outs(values: list[Interval]) -> list[IntervalOut]:
    return [_iv_out(v) for v in values]


def _total(values: list[Interval]) -> int:
    return sum(v.end - v.start for v in values)


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


class InvalidReferenceError(ValueError):
    pass


@dataclass(frozen=True)
class RepairRefData:
    repair_no: str
    occurred_at: datetime
    interval: Interval
    overlap: Interval


@dataclass(frozen=True)
class Evidence:
    ndt_no: str
    weld_no: str
    completed_at: datetime | None
    interval: Interval
    trusted: bool
    reason: str
    effective: list[Interval]
    invalidating_repairs: list[RepairRefData]


@dataclass(frozen=True)
class CertSnapshot:
    cert_no: str
    weld_no: str
    issued_at: datetime
    approved: Interval
    cited: list[str]
    evidence: list[Evidence]

    @property
    def gaps(self) -> list[Interval]:
        trusted = [iv for ev in self.evidence for iv in ev.effective]
        return subtract_all([self.approved], trusted)


@dataclass(frozen=True)
class RepairProbe:
    weld_no: str
    occurred_at: datetime
    interval: Interval


def _load_cert(conn: sqlite3.Connection, cert_no: str) -> CertSnapshot:
    row = conn.execute(
        "SELECT cert_no, weld_no, issued_at, start_mm, end_mm FROM certificates WHERE cert_no = ?",
        (cert_no,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"certificate {cert_no!r} not found")
    cited = [
        r["ndt_no"]
        for r in conn.execute(
            "SELECT ndt_no FROM certificate_citations WHERE cert_no = ? ORDER BY position",
            (cert_no,),
        )
    ]
    issued_at = from_ts(row["issued_at"])
    approved = _iv(row["start_mm"], row["end_mm"])

    evidence: list[Evidence] = []
    for ndt_no in cited:
        ndt = conn.execute(
            "SELECT order_no, weld_no, completed_at, start_mm, end_mm FROM ndt_orders "
            "WHERE order_no = ?",
            (ndt_no,),
        ).fetchone()
        if ndt is None:
            # The FK prevents this; keep the report honest if an archive was
            # loaded by other means.
            evidence.append(Evidence(ndt_no, "", None, approved, False, "missing", [], []))
            continue

        completed = from_ts(ndt["completed_at"])
        coverage = _iv(ndt["start_mm"], ndt["end_mm"])
        if ndt["weld_no"] != row["weld_no"]:
            evidence.append(
                Evidence(
                    ndt_no,
                    ndt["weld_no"],
                    completed,
                    coverage,
                    False,
                    "weld_mismatch",
                    [],
                    [],
                )
            )
            continue
        if not completed < issued_at:
            evidence.append(
                Evidence(
                    ndt_no,
                    ndt["weld_no"],
                    completed,
                    coverage,
                    False,
                    "not_completed_before_issuance",
                    [],
                    [],
                )
            )
            continue

        clipped = coverage.intersection(approved)
        if clipped is None:
            # Trusted, but wholly outside what this certificate approves, so
            # it cannot back any part of the certificate.
            evidence.append(
                Evidence(
                    ndt_no,
                    ndt["weld_no"],
                    completed,
                    coverage,
                    True,
                    "outside_approved_interval",
                    [],
                    [],
                )
            )
            continue

        repairs = conn.execute(
            "SELECT order_no, occurred_at, start_mm, end_mm FROM repairs "
            "WHERE weld_no = ? AND occurred_at > ? AND occurred_at < ? "
            "ORDER BY occurred_at, order_no",
            (row["weld_no"], ndt["completed_at"], row["issued_at"]),
        ).fetchall()
        cuts: list[Interval] = []
        refs: list[RepairRefData] = []
        for rep in repairs:
            rep_iv = _iv(rep["start_mm"], rep["end_mm"])
            overlap = clipped.intersection(rep_iv)
            if overlap is None:
                continue
            cuts.append(overlap)
            refs.append(
                RepairRefData(
                    rep["order_no"],
                    from_ts(rep["occurred_at"]),
                    rep_iv,
                    overlap,
                )
            )

        evidence.append(
            Evidence(
                ndt_no,
                ndt["weld_no"],
                completed,
                coverage,
                True,
                "ok",
                subtract_all([clipped], cuts),
                refs,
            )
        )
    return CertSnapshot(row["cert_no"], row["weld_no"], issued_at, approved, cited, evidence)


def _evidence_out(ev: Evidence) -> EvidenceOut:
    return EvidenceOut(
        ndt_no=ev.ndt_no,
        weld_no=ev.weld_no,
        completed_at=None if ev.completed_at is None else to_ts(ev.completed_at),
        interval=_iv_out(ev.interval),
        trusted=ev.trusted,
        reason=ev.reason,  # type: ignore[arg-type]
        effective_coverage=_iv_outs(ev.effective),
        invalidating_repairs=[
            RepairRef(
                repair_no=r.repair_no,
                occurred_at=to_ts(r.occurred_at),
                interval=_iv_out(r.interval),
                overlap=_iv_out(r.overlap),
            )
            for r in ev.invalidating_repairs
        ],
    )


def _gaps_report(conn: sqlite3.Connection, cert_no: str) -> GapReport:
    snap = _load_cert(conn, cert_no)
    gaps = snap.gaps
    return GapReport(
        cert_no=snap.cert_no,
        weld_no=snap.weld_no,
        issued_at=to_ts(snap.issued_at),
        approved=_iv_out(snap.approved),
        gaps=_iv_outs(gaps),
        gap_total_mm=_total(gaps),
        evidence=[_evidence_out(ev) for ev in snap.evidence],
    )


def check_gap(conn: sqlite3.Connection, cert_no: str) -> GapReport:
    with read_tx(conn):
        return _gaps_report(conn, cert_no)


def list_certificates(conn: sqlite3.Connection) -> list[CertificateOut]:
    with read_tx(conn):
        cert_nos = [
            r["cert_no"] for r in conn.execute("SELECT cert_no FROM certificates ORDER BY cert_no")
        ]
        out: list[CertificateOut] = []
        for cert_no in cert_nos:
            snap = _load_cert(conn, cert_no)
            out.append(
                CertificateOut(
                    cert_no=snap.cert_no,
                    weld_no=snap.weld_no,
                    issued_at=to_ts(snap.issued_at),
                    approved=_iv_out(snap.approved),
                    ndt_order_nos=snap.cited,
                )
            )
        return out


# --------------------------------------------------------------------------
# Reference creation
# --------------------------------------------------------------------------


def create_ndt(conn: sqlite3.Connection, body: NdtOrderIn) -> NdtOrderOut:
    with write_tx(conn):
        if conn.execute("SELECT 1 FROM ndt_orders WHERE order_no = ?", (body.order_no,)).fetchone():
            raise ConflictError(f"NDT order {body.order_no!r} already exists")
        conn.execute(
            "INSERT INTO ndt_orders (order_no, weld_no, completed_at, start_mm, end_mm) "
            "VALUES (?, ?, ?, ?, ?)",
            (body.order_no, body.weld_no, to_ts(body.completed_at), body.start_mm, body.end_mm),
        )
    return NdtOrderOut(
        order_no=body.order_no,
        weld_no=body.weld_no,
        completed_at=to_ts(body.completed_at),
        interval=_iv_out(_iv(body.start_mm, body.end_mm)),
    )


def create_certificate(conn: sqlite3.Connection, body: CertificateIn) -> CertificateOut:
    with write_tx(conn):
        if conn.execute("SELECT 1 FROM certificates WHERE cert_no = ?", (body.cert_no,)).fetchone():
            raise ConflictError(f"certificate {body.cert_no!r} already exists")
        missing = [
            no
            for no in body.ndt_order_nos
            if not conn.execute("SELECT 1 FROM ndt_orders WHERE order_no = ?", (no,)).fetchone()
        ]
        if missing:
            raise InvalidReferenceError(f"unknown NDT order(s): {', '.join(missing)}")
        conn.execute(
            "INSERT INTO certificates (cert_no, weld_no, issued_at, start_mm, end_mm) "
            "VALUES (?, ?, ?, ?, ?)",
            (body.cert_no, body.weld_no, to_ts(body.issued_at), body.start_mm, body.end_mm),
        )
        conn.executemany(
            "INSERT INTO certificate_citations (cert_no, ndt_no, position) VALUES (?, ?, ?)",
            [(body.cert_no, no, i) for i, no in enumerate(body.ndt_order_nos)],
        )
    return CertificateOut(
        cert_no=body.cert_no,
        weld_no=body.weld_no,
        issued_at=to_ts(body.issued_at),
        approved=_iv_out(_iv(body.start_mm, body.end_mm)),
        ndt_order_nos=body.ndt_order_nos,
    )


# --------------------------------------------------------------------------
# Repair ingest
# --------------------------------------------------------------------------


def _candidate_certs(
    conn: sqlite3.Connection, probes: list[RepairProbe]
) -> dict[str, CertSnapshot]:
    """Certificates a probe can truncate, with their pre-write snapshot."""
    welds = sorted({p.weld_no for p in probes})
    if not welds:
        return {}
    placeholders = ",".join("?" for _ in welds)
    rows = conn.execute(
        f"SELECT cert_no FROM certificates WHERE weld_no IN ({placeholders}) ORDER BY cert_no",
        welds,
    ).fetchall()

    found: dict[str, CertSnapshot] = {}
    for row in rows:
        snap = _load_cert(conn, row["cert_no"])
        for probe in probes:
            if probe.weld_no != snap.weld_no:
                continue
            for ev in snap.evidence:
                if not ev.trusted or ev.completed_at is None:
                    continue
                if not (ev.completed_at < probe.occurred_at < snap.issued_at):
                    continue
                clipped = ev.interval.intersection(snap.approved)
                if clipped is None or not clipped.intersects(probe.interval):
                    continue
                found[snap.cert_no] = snap
                break
            if snap.cert_no in found:
                break
    return found


def _changed_certificates(
    conn: sqlite3.Connection, before: dict[str, CertSnapshot]
) -> list[ChangedCertificate]:
    changed: list[ChangedCertificate] = []
    for cert_no in sorted(before):
        snap_before = before[cert_no]
        after = _load_cert(conn, cert_no).gaps
        prior = snap_before.gaps
        if prior == after:
            continue
        changed.append(
            ChangedCertificate(
                cert_no=cert_no,
                weld_no=snap_before.weld_no,
                gaps_before=_iv_outs(prior),
                gaps_after=_iv_outs(after),
                gap_total_before_mm=_total(prior),
                gap_total_after_mm=_total(after),
            )
        )
    return changed


def _audit(
    conn: sqlite3.Connection,
    repair_no: str,
    action: str,
    weld_no: str,
    occurred_at: datetime,
    start_mm: int,
    end_mm: int,
    candidates_checked: int,
    changed: list[ChangedCertificate],
) -> int:
    cur = conn.execute(
        "INSERT INTO repair_ingest_audit (repair_no, action, weld_no, occurred_at, "
        "start_mm, end_mm, candidates_checked, changed_certificates, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            repair_no,
            action,
            weld_no,
            to_ts(occurred_at),
            start_mm,
            end_mm,
            candidates_checked,
            json.dumps([c.model_dump() for c in changed], ensure_ascii=False),
            to_ts(datetime.now(UTC)),
        ),
    )
    return int(cur.lastrowid)


def _repair_out(repair_no: str, body: RepairIn | RepairUpdateIn) -> RepairOut:
    return RepairOut(
        order_no=repair_no,
        weld_no=body.weld_no,
        occurred_at=to_ts(body.occurred_at),
        interval=_iv_out(_iv(body.start_mm, body.end_mm)),
    )


def ingest_repair(conn: sqlite3.Connection, body: RepairIn) -> RepairIngestResult:
    with write_tx(conn):
        if conn.execute("SELECT 1 FROM repairs WHERE order_no = ?", (body.order_no,)).fetchone():
            raise ConflictError(f"repair {body.order_no!r} already exists; correct it with PUT")

        probe = RepairProbe(body.weld_no, body.occurred_at, _iv(body.start_mm, body.end_mm))
        before = _candidate_certs(conn, [probe])
        conn.execute(
            "INSERT INTO repairs (order_no, weld_no, occurred_at, start_mm, end_mm) "
            "VALUES (?, ?, ?, ?, ?)",
            (body.order_no, body.weld_no, to_ts(body.occurred_at), body.start_mm, body.end_mm),
        )
        changed = _changed_certificates(conn, before)
        audit_id = _audit(
            conn,
            body.order_no,
            "created",
            body.weld_no,
            body.occurred_at,
            body.start_mm,
            body.end_mm,
            len(before),
            changed,
        )
    return RepairIngestResult(
        repair=_repair_out(body.order_no, body),
        action="created",
        candidates_checked=len(before),
        changed_count=len(changed),
        changed_certificates=changed,
        audit_id=audit_id,
    )


def correct_repair(
    conn: sqlite3.Connection, repair_no: str, body: RepairUpdateIn
) -> RepairIngestResult:
    with write_tx(conn):
        old = conn.execute(
            "SELECT order_no, weld_no, occurred_at, start_mm, end_mm FROM repairs "
            "WHERE order_no = ?",
            (repair_no,),
        ).fetchone()
        if old is None:
            raise NotFoundError(f"repair {repair_no!r} not found")

        old_probe = RepairProbe(
            old["weld_no"], from_ts(old["occurred_at"]), _iv(old["start_mm"], old["end_mm"])
        )
        new_probe = RepairProbe(body.weld_no, body.occurred_at, _iv(body.start_mm, body.end_mm))
        unchanged = old_probe == new_probe
        # The old version and the new version are both considered: removing the
        # old cut or adding the new cut are the only ways a gap can move.
        before = _candidate_certs(conn, [old_probe, new_probe])

        if unchanged:
            audit_id = _audit(
                conn,
                repair_no,
                "unchanged",
                body.weld_no,
                body.occurred_at,
                body.start_mm,
                body.end_mm,
                len(before),
                [],
            )
            return RepairIngestResult(
                repair=_repair_out(repair_no, body),
                action="unchanged",
                candidates_checked=len(before),
                changed_count=0,
                changed_certificates=[],
                audit_id=audit_id,
            )

        conn.execute(
            "UPDATE repairs SET weld_no = ?, occurred_at = ?, start_mm = ?, end_mm = ? "
            "WHERE order_no = ?",
            (body.weld_no, to_ts(body.occurred_at), body.start_mm, body.end_mm, repair_no),
        )
        changed = _changed_certificates(conn, before)
        audit_id = _audit(
            conn,
            repair_no,
            "corrected",
            body.weld_no,
            body.occurred_at,
            body.start_mm,
            body.end_mm,
            len(before),
            changed,
        )
    return RepairIngestResult(
        repair=_repair_out(repair_no, body),
        action="corrected",
        candidates_checked=len(before),
        changed_count=len(changed),
        changed_certificates=changed,
        audit_id=audit_id,
    )


def list_repairs(conn: sqlite3.Connection) -> list[RepairOut]:
    with read_tx(conn):
        rows = conn.execute(
            "SELECT order_no, weld_no, occurred_at, start_mm, end_mm FROM repairs ORDER BY order_no"
        ).fetchall()
        return [
            RepairOut(
                order_no=r["order_no"],
                weld_no=r["weld_no"],
                occurred_at=r["occurred_at"],
                interval=_iv_out(_iv(r["start_mm"], r["end_mm"])),
            )
            for r in rows
        ]
