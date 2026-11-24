"""FastAPI application for retroactive repair-order gap checks."""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import service
from .db import connect, init_db
from .models import (
    CertificateIn,
    CertificateOut,
    GapReport,
    HealthOut,
    NdtOrderIn,
    NdtOrderOut,
    RepairIn,
    RepairIngestResult,
    RepairOut,
    RepairUpdateIn,
)

DEFAULT_DB_PATH = os.environ.get("ARCHIVE_DB", "/data/archive.db")


def _db_conn(request: Request) -> sqlite3.Connection:
    return connect(request.app.state.db_path)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_path = os.environ.get("ARCHIVE_DB", DEFAULT_DB_PATH)
    init_db(app.state.db_path)
    yield


app = FastAPI(
    title="Weld Certificate Gap Check",
    version="1.0.0",
    summary=(
        "Backfill/correct repair orders and report which earlier weld certificates "
        "lost NDT evidence."
    ),
    description=(
        "All intervals are half-open `[start_mm, end_mm)` in millimetres.\n\n"
        "**Gap check**: `GET /certificates/{cert_no}/gaps` counts only cited, "
        "same-weld NDT orders completed before issuance, clipped to the approved "
        "interval. A repair between film and issuance invalidates only the "
        "intersecting part.\n\n"
        "**Repair ingest**: `POST /repairs` backfills; `PUT /repairs/{repair_no}` "
        "corrects under the same number. Both re-check only certificates the old "
        "or new repair can truncate and return the changed cert numbers with "
        "before/after gaps. Repair and audit are committed atomically."
    ),
    openapi_tags=[
        {"name": "check", "description": "查证书缺口（只采信证上引用、签发前完成的同缝探伤）"},
        {"name": "repairs", "description": "补录 / 同号更正返修单，返回缺口变化的证号与前后缺口"},
        {"name": "reference", "description": "探伤单与合格证建档"},
        {"name": "ops", "description": "健康检查"},
    ],
    lifespan=lifespan,
)


@app.exception_handler(service.NotFoundError)
async def _not_found(_: Request, exc: service.NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(service.ConflictError)
async def _conflict(_: Request, exc: service.ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(service.InvalidReferenceError)
async def _invalid(_: Request, exc: service.InvalidReferenceError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.get("/healthz", response_model=HealthOut, tags=["ops"])
def healthz(request: Request) -> HealthOut:
    conn = _db_conn(request)
    try:
        conn.execute("SELECT 1 FROM certificates LIMIT 1").fetchall()
    finally:
        conn.close()
    return HealthOut(status="ok", db_path=request.app.state.db_path)


# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------


@app.post("/ndt-orders", response_model=NdtOrderOut, status_code=201, tags=["reference"])
def post_ndt_order(body: NdtOrderIn, conn: sqlite3.Connection = Depends(_db_conn)):
    """Record a completed NDT order (the 'film')."""
    try:
        return service.create_ndt(conn, body)
    finally:
        conn.close()


@app.post("/certificates", response_model=CertificateOut, status_code=201, tags=["reference"])
def post_certificate(body: CertificateIn, conn: sqlite3.Connection = Depends(_db_conn)):
    """Issue a certificate citing the NDT orders it was based on."""
    try:
        return service.create_certificate(conn, body)
    finally:
        conn.close()


@app.get("/certificates", response_model=list[CertificateOut], tags=["reference"])
def get_certificates(conn: sqlite3.Connection = Depends(_db_conn)):
    try:
        return service.list_certificates(conn)
    finally:
        conn.close()


@app.get("/repairs", response_model=list[RepairOut], tags=["repairs"])
def get_repairs(conn: sqlite3.Connection = Depends(_db_conn)):
    try:
        return service.list_repairs(conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Gap check
# --------------------------------------------------------------------------


@app.get(
    "/certificates/{cert_no}/gaps",
    response_model=GapReport,
    tags=["check"],
    summary="查某张合格证的缺口",
)
def get_gaps(cert_no: str, conn: sqlite3.Connection = Depends(_db_conn)):
    """Report gaps in the approved interval of one certificate.

    Only cited, same-weld NDT orders completed before issuance count.  A
    repair between film and issuance invalidates only the intersecting part.
    Every cited order is listed with whether it was trusted, why not, and which
    repairs cut which sub-intervals.
    """
    try:
        return service.check_gap(conn, cert_no)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Repair ingest
# --------------------------------------------------------------------------


@app.post(
    "/repairs",
    response_model=RepairIngestResult,
    status_code=201,
    tags=["repairs"],
    summary="补录返修单",
)
def post_repair(body: RepairIn, conn: sqlite3.Connection = Depends(_db_conn)):
    """Backfill a repair order; returns certificates whose gaps changed.

    Only certificates whose cited trusted evidence this repair can truncate are
    re-checked.  The repair row and its audit record commit atomically.
    """
    try:
        return service.ingest_repair(conn, body)
    finally:
        conn.close()


@app.put(
    "/repairs/{repair_no}",
    response_model=RepairIngestResult,
    tags=["repairs"],
    summary="同号更正返修单",
)
def put_repair(
    repair_no: str,
    body: RepairUpdateIn,
    conn: sqlite3.Connection = Depends(_db_conn),
):
    """Correct a repair under the same number; returns before/after gaps.

    Candidates are the union of what the old and the new version can truncate,
    so shrinking or moving a repair correctly reports restored gaps.
    """
    try:
        return service.correct_repair(conn, repair_no, body)
    finally:
        conn.close()
