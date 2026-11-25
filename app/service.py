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
    CertificateLookupOut,
    CertificateOut,
    ChangedCertificate,
    EvidenceOut,
    GapIntervalIn,
    GapReport,
    IntervalOut,
    NdtOrderIn,
    NdtOrderOut,
    RepairIn,
    RepairIngestResult,
    RepairLookupEchoOut,
    RepairLookupOut,
    RepairLookupRequest,
    RepairOut,
    RepairRef,
    RepairUpdateIn,
    TimeIntervalOut,
    TimeSolutionsOut,
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


@dataclass(frozen=True)
class RepairLookupProbe:
    """A repair whose time is being solved for, with every other field fixed."""

    repair_no: str
    weld_no: str
    interval: Interval


def _load_cert(
    conn: sqlite3.Connection,
    cert_no: str,
    *,
    probe: RepairLookupProbe | None = None,
    at: datetime | None = None,
) -> CertSnapshot:
    """Load one certificate's evidence and gaps.

    With ``probe``/``at`` the chosen repair is moved to ``at`` and dropped from
    the archive's repair list before invalidations are applied; every other
    repair keeps its recorded time and interval.  This is the same evidence and
    invalidation logic as the read-only gap report.
    """
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
        candidates: list[tuple[str, datetime, Interval]] = []
        for rep in repairs:
            if probe is not None and rep["order_no"] == probe.repair_no:
                continue
            candidates.append(
                (rep["order_no"], from_ts(rep["occurred_at"]), _iv(rep["start_mm"], rep["end_mm"]))
            )
        if probe is not None and probe.weld_no == row["weld_no"]:
            # The reverse lookup replaces just this repair's time; when ``at``
            # is None the repair is left out of the base snapshot entirely.
            if at is not None and completed < at < issued_at:
                candidates.append((probe.repair_no, at, probe.interval))
            candidates.sort(key=lambda item: (item[1], item[0]))

        cuts: list[Interval] = []
        refs: list[RepairRefData] = []
        for rep_no, rep_time, rep_iv in candidates:
            overlap = clipped.intersection(rep_iv)
            if overlap is None:
                continue
            cuts.append(overlap)
            refs.append(RepairRefData(rep_no, rep_time, rep_iv, overlap))

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


# --------------------------------------------------------------------------
# Reverse lookup: which repair times reproduce the recorded gaps?
#
# The repair's number, weld and interval stay as archived; only its
# ``occurred_at`` is solved for, and every other repair keeps its recorded
# time.  Under the read-path rules a repair can only invalidate an evidence
# piece while its time is strictly inside that piece's
# ``(completed, issued)`` window, and the cut it makes is always the same
# ``interval``.  A certificate's gap set is therefore constant on every open
# span between those window boundaries, and each boundary instant needs its
# own check because the rule is strict at both ends.  Sweeping those finitely
# many instants is exact; no second-by-second or microsecond-by-microsecond
# trial is needed.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _TimeSpan:
    """A time range; endpoints are instants at microsecond resolution."""

    start: datetime
    end: datetime
    start_inclusive: bool
    end_inclusive: bool


@dataclass(frozen=True)
class _Solution:
    spaces: list[_TimeSpan]
    instants: list[datetime]

    @property
    def empty(self) -> bool:
        return not self.spaces and not self.instants


def _merge_ivs(values: list[Interval]) -> list[Interval]:
    """Sort and union half-open millimetre intervals."""
    if not values:
        return []
    merged = [sorted(values)[0]]
    for item in sorted(values)[1:]:
        last = merged[-1]
        if item.start <= last.end:  # touching pieces are contiguous
            merged[-1] = Interval(last.start, max(last.end, item.end))
        else:
            merged.append(item)
    return merged


def _same_gaps(left: list[Interval], right: list[Interval]) -> bool:
    return _merge_ivs(left) == _merge_ivs(right)


def _expected_intervals(items: list[GapIntervalIn]) -> list[Interval]:
    return _merge_ivs([Interval(item.start_mm, item.end_mm) for item in items])


def _gaps_with_active(snap: CertSnapshot, active: set[str], repair_iv: Interval) -> list[Interval]:
    """Gaps when the moved repair applies to exactly the ``active`` orders."""
    coverage: list[Interval] = []
    for ev in snap.evidence:
        if not ev.trusted:
            continue
        pieces = ev.effective
        if ev.ndt_no in active:
            pieces = subtract_all(pieces, [repair_iv])
        coverage.extend(pieces)
    return _merge_ivs(subtract_all([snap.approved], coverage))


def _runs(points: list[datetime], point_ok: list[bool], span_ok: list[bool]) -> _Solution:
    """Assemble maximal matching runs from alternating points and open spans.

    ``points`` are sorted instants; ``point_ok[i]`` says instant ``i``
    matches and ``span_ok[i]`` says every time strictly between ``points[i]``
    and ``points[i + 1]`` matches.  A run made of one point becomes an
    isolated instant; a run containing a span becomes a range whose endpoint
    flags say whether the boundary instants belong to it.
    """
    pieces: list[tuple[str, int]] = []
    for i in range(len(points)):
        pieces.append(("point", i))
        if i < len(points) - 1:
            pieces.append(("span", i))

    def ok(piece: tuple[str, int]) -> bool:
        return point_ok[piece[1]] if piece[0] == "point" else span_ok[piece[1]]

    spaces: list[_TimeSpan] = []
    instants: list[datetime] = []
    i = 0
    while i < len(pieces):
        if not ok(pieces[i]):
            i += 1
            continue
        j = i
        while j + 1 < len(pieces) and ok(pieces[j + 1]):
            j += 1
        group = pieces[i : j + 1]
        if len(group) == 1 and group[0][0] == "point":
            instants.append(points[group[0][1]])
        else:
            first_kind, first_idx = group[0]
            last_kind, last_idx = group[-1]
            lo = points[first_idx]
            hi = points[last_idx] if last_kind == "point" else points[last_idx + 1]
            spaces.append(_TimeSpan(lo, hi, first_kind == "point", last_kind == "point"))
        i = j + 1
    return _Solution(spaces, instants)


def _span_contains(span: _TimeSpan, moment: datetime) -> bool:
    if span.start < moment < span.end:
        return True
    if moment == span.start:
        return span.start_inclusive
    if moment == span.end:
        return span.end_inclusive
    return False


def _contains(solution: _Solution, moment: datetime) -> bool:
    return moment in solution.instants or any(
        _span_contains(span, moment) for span in solution.spaces
    )


def _open_contained(solution: _Solution, lo: datetime, hi: datetime) -> bool:
    return any(span.start <= lo and hi <= span.end for span in solution.spaces)


def _intersect(left: _Solution, right: _Solution) -> _Solution:
    """Intersection of two time sets, exact at shared boundaries."""
    points = sorted(
        {span.start for span in left.spaces}
        | {span.end for span in left.spaces}
        | {span.start for span in right.spaces}
        | {span.end for span in right.spaces}
        | set(left.instants)
        | set(right.instants)
    )
    if not points:
        return _Solution([], [])
    point_ok = [_contains(left, p) and _contains(right, p) for p in points]
    span_ok = [
        _open_contained(left, points[i], points[i + 1])
        and _open_contained(right, points[i], points[i + 1])
        for i in range(len(points) - 1)
    ]
    return _runs(points, point_ok, span_ok)


def _cert_time_solutions(
    snap: CertSnapshot,
    probe: RepairLookupProbe,
    expected: list[Interval],
    search: tuple[datetime, datetime],
) -> _Solution:
    """Every time in the inclusive ``search`` window reproducing ``expected``.

    ``snap`` must already exclude the moved repair; other repairs are baked
    into each order's effective coverage.  Moving the repair changes the gaps
    only through the orders whose effective pieces it can cut, and only when
    its time is strictly inside those orders' ``(completed, issued)``
    windows.  Those window edges and the search bounds are therefore the only
    instants at which the match result can change.  Each boundary instant is
    checked on its own; on an open span between consecutive boundaries the
    answer is constant and is evaluated analytically, so the sweep covers the
    whole window exactly without sampling it.
    """
    a, b = search
    same_weld = probe.weld_no == snap.weld_no
    cuttable = [
        ev
        for ev in snap.evidence
        if same_weld
        and ev.trusted
        and ev.completed_at is not None
        and any(piece.intersects(probe.interval) for piece in ev.effective)
    ]

    boundaries = {a, b}
    for ev in cuttable:
        completed = ev.completed_at
        assert completed is not None
        for moment in (completed, snap.issued_at):
            if a < moment < b:
                boundaries.add(moment)
    points = sorted(boundaries)
    def active_at(moment: datetime) -> set[str]:
        return {
            ev.ndt_no
            for ev in cuttable
            if ev.completed_at is not None and ev.completed_at < moment < snap.issued_at
        }

    def active_span(lo: datetime, hi: datetime) -> set[str]:
        # No edge lies strictly inside, so validity is constant over the span:
        # valid exactly when the whole span is inside the strict window.
        return {
            ev.ndt_no
            for ev in cuttable
            if ev.completed_at is not None and ev.completed_at <= lo and hi <= snap.issued_at
        }

    point_ok = [
        _same_gaps(_gaps_with_active(snap, active_at(p), probe.interval), expected) for p in points
    ]
    span_ok = [
        _same_gaps(
            _gaps_with_active(snap, active_span(points[i], points[i + 1]), probe.interval),
            expected,
        )
        for i in range(len(points) - 1)
    ]
    return _runs(points, point_ok, span_ok)


def _solutions_out(solution: _Solution) -> TimeSolutionsOut:
    return TimeSolutionsOut(
        intervals=[
            TimeIntervalOut(
                start=to_ts(span.start),
                end=to_ts(span.end),
                start_inclusive=span.start_inclusive,
                end_inclusive=span.end_inclusive,
            )
            for span in solution.spaces
        ],
        instants=[to_ts(moment) for moment in solution.instants],
    )


def lookup_repair_time(
    conn: sqlite3.Connection, repair_no: str, body: RepairLookupRequest
) -> RepairLookupOut:
    """Read-only reverse lookup of one repair's ``occurred_at``.

    Everything is read inside one deferred (snapshot) transaction, so the
    answer comes from a single committed archive state even while other
    requests keep backfilling or correcting repairs.  Only the named
    certificates and the archive rows needed to re-derive their evidence are
    loaded; no row is written and no audit trail is produced.
    """
    with read_tx(conn):
        repair = conn.execute(
            "SELECT order_no, weld_no, occurred_at, start_mm, end_mm FROM repairs "
            "WHERE order_no = ?",
            (repair_no,),
        ).fetchone()
        if repair is None:
            raise NotFoundError(f"repair {repair_no!r} not found")

        probe = RepairLookupProbe(
            repair_no, repair["weld_no"], _iv(repair["start_mm"], repair["end_mm"])
        )
        window = (body.window_start, body.window_end)

        cert_out: list[CertificateLookupOut] = []
        per_cert: list[_Solution] = []
        for wanted in body.certificates:
            row = conn.execute(
                "SELECT cert_no FROM certificates WHERE cert_no = ?", (wanted.cert_no,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"certificate {wanted.cert_no!r} not found")
            expected = _expected_intervals(wanted.gaps)
            # The chosen repair is left out of the base snapshot; its time is
            # what the search replaces.
            snap = _load_cert(conn, wanted.cert_no, probe=probe, at=None)
            solution = _cert_time_solutions(snap, probe, expected, window)
            per_cert.append(solution)
            cert_out.append(
                CertificateLookupOut(
                    cert_no=snap.cert_no,
                    weld_no=snap.weld_no,
                    expected_gaps=_iv_outs(expected),
                    has_solution=not solution.empty,
                    solutions=_solutions_out(solution),
                )
            )

        common = per_cert[0]
        for solution in per_cert[1:]:
            common = _intersect(common, solution)

        if any(solution.empty for solution in per_cert):
            status = "unsatisfiable"
        elif common.empty:
            status = "no_common_time"
        else:
            status = "found"

        return RepairLookupOut(
            repair=RepairLookupEchoOut(
                repair_no=probe.repair_no,
                weld_no=probe.weld_no,
                recorded_occurred_at=repair["occurred_at"],
                interval=_iv_out(probe.interval),
            ),
            query=TimeIntervalOut(
                start=to_ts(window[0]),
                end=to_ts(window[1]),
                start_inclusive=True,
                end_inclusive=True,
            ),
            status=status,
            common=_solutions_out(common),
            certificates=cert_out,
        )
