"""SQLite access helpers.

The archive is a single SQLite file.  Connections run in autocommit mode so
that every write endpoint can take an explicit ``BEGIN IMMEDIATE`` transaction
and commit or roll back as one unit.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS ndt_orders (
    order_no      TEXT PRIMARY KEY,
    weld_no       TEXT NOT NULL,
    completed_at  TEXT NOT NULL,
    start_mm      INTEGER NOT NULL,
    end_mm        INTEGER NOT NULL,
    CHECK (end_mm > start_mm)
);

CREATE TABLE IF NOT EXISTS certificates (
    cert_no    TEXT PRIMARY KEY,
    weld_no    TEXT NOT NULL,
    issued_at  TEXT NOT NULL,
    start_mm   INTEGER NOT NULL,
    end_mm     INTEGER NOT NULL,
    CHECK (end_mm > start_mm)
);

CREATE TABLE IF NOT EXISTS certificate_citations (
    cert_no  TEXT NOT NULL REFERENCES certificates(cert_no) ON DELETE CASCADE,
    ndt_no   TEXT NOT NULL REFERENCES ndt_orders(order_no),
    position INTEGER NOT NULL,
    PRIMARY KEY (cert_no, ndt_no)
);

CREATE TABLE IF NOT EXISTS repairs (
    order_no     TEXT PRIMARY KEY,
    weld_no      TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    start_mm     INTEGER NOT NULL,
    end_mm       INTEGER NOT NULL,
    CHECK (end_mm > start_mm)
);

CREATE TABLE IF NOT EXISTS repair_ingest_audit (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    repair_no            TEXT NOT NULL,
    action               TEXT NOT NULL,
    weld_no              TEXT NOT NULL,
    occurred_at          TEXT NOT NULL,
    start_mm             INTEGER NOT NULL,
    end_mm               INTEGER NOT NULL,
    candidates_checked   INTEGER NOT NULL,
    changed_certificates TEXT NOT NULL,
    created_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ndt_weld_time
    ON ndt_orders (weld_no, completed_at);
CREATE INDEX IF NOT EXISTS idx_cert_weld_time
    ON certificates (weld_no, issued_at);
CREATE INDEX IF NOT EXISTS idx_repair_weld_time
    ON repairs (weld_no, occurred_at);
CREATE INDEX IF NOT EXISTS idx_citation_ndt
    ON certificate_citations (ndt_no);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str | Path) -> None:
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One atomic write unit.  Any exception rolls the whole unit back."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Stable snapshot for multi-statement reports."""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
