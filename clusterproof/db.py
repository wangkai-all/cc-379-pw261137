"""SQLite connection handling and schema for ClusterProof.

The database is intentionally the only shared state. Each service opens its
own connection; WAL plus a busy timeout keeps the three processes from
stepping on each other during the interleaved demo.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DEFAULT_DB = os.environ.get("CLUSTERPROOF_DB", "var/clusterproof.sqlite3")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ---------------------------------------------------------------- source data
CREATE TABLE IF NOT EXISTS accounts (
    account_id      INTEGER PRIMARY KEY,
    display_name    TEXT NOT NULL,
    segment         TEXT NOT NULL,
    region          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    withdrawn_at    TEXT,
    withdrawal_note TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    order_id     INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(account_id),
    total_cents  INTEGER NOT NULL,
    placed_on    TEXT NOT NULL,
    state        TEXT NOT NULL,
    withdrawn_at TEXT
);

CREATE TABLE IF NOT EXISTS order_lines (
    line_id       INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(order_id),
    sku           TEXT NOT NULL,
    qty           INTEGER NOT NULL,
    unit_cents    INTEGER NOT NULL,
    withdrawn_at  TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
    contact_id   INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(account_id),
    channel      TEXT NOT NULL,
    value        TEXT NOT NULL,
    withdrawn_at TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    note_id      INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(account_id),
    body         TEXT NOT NULL,
    created_on   TEXT NOT NULL,
    withdrawn_at TEXT
);

-- ------------------------------------------------------------- declarations
-- What a sample of accounts is required to drag along.
CREATE TABLE IF NOT EXISTS relations (
    child_table TEXT PRIMARY KEY,
    parent_table TEXT NOT NULL,
    parent_col  TEXT NOT NULL,
    required    INTEGER NOT NULL DEFAULT 1,
    enabled     INTEGER NOT NULL DEFAULT 1,
    changed_at  TEXT NOT NULL
);

-- Stratification: every published sample must cover these cells.
CREATE TABLE IF NOT EXISTS strata (
    stratum_id   TEXT PRIMARY KEY,
    segment      TEXT NOT NULL,
    region       TEXT NOT NULL,
    min_rows     INTEGER NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    changed_at   TEXT NOT NULL
);

-- --------------------------------------------------------------- sample runs
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    label         TEXT NOT NULL,
    seed          TEXT NOT NULL,
    sample_size   INTEGER NOT NULL,
    salt          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    mode          TEXT NOT NULL,          -- initial | topup
    parent_run_id TEXT,
    state         TEXT NOT NULL,          -- building | checked | published | rejected
    notes         TEXT
);

-- One row per attempt to build a candidate from a run.
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id     TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES runs(run_id),
    built_at         TEXT NOT NULL,
    payload          TEXT NOT NULL,       -- canonical JSON of the frozen sample
    payload_hash     TEXT NOT NULL,
    member_count     INTEGER NOT NULL,
    row_count        INTEGER NOT NULL,
    selector_version TEXT NOT NULL,
    state            TEXT NOT NULL,       -- draft | checked | published | rejected
    observation      TEXT NOT NULL DEFAULT '{}',
    change_cursor    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS candidate_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    at           TEXT NOT NULL,
    kind         TEXT NOT NULL,
    detail       TEXT NOT NULL
);

-- ------------------------------------------------------------------ releases
CREATE TABLE IF NOT EXISTS releases (
    release_id    TEXT PRIMARY KEY,
    candidate_id  TEXT NOT NULL REFERENCES candidates(candidate_id),
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    published_at  TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    signature     TEXT NOT NULL,
    manifest      TEXT NOT NULL,
    supersedes    TEXT,
    change_cursor INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS release_rows (
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    table_name TEXT NOT NULL,
    row_key    TEXT NOT NULL,
    row_json   TEXT NOT NULL,
    stratum_id TEXT,
    PRIMARY KEY (release_id, table_name, row_key)
);

-- Withdrawals/enabled-flag flips observed by the sampler. Used to explain
-- member churn and to refuse re-publishing withdrawn data.
CREATE TABLE IF NOT EXISTS source_changes (
    change_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,       -- withdrawal | relation | stratum | insert
    target      TEXT NOT NULL,
    detail      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    subject    TEXT NOT NULL,
    detail     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_account ON orders(account_id);
CREATE INDEX IF NOT EXISTS idx_lines_order ON order_lines(order_id);
CREATE INDEX IF NOT EXISTS idx_contacts_account ON contacts(account_id);
CREATE INDEX IF NOT EXISTS idx_notes_account ON notes(account_id);
CREATE INDEX IF NOT EXISTS idx_candidate_events ON candidate_events(candidate_id);
CREATE INDEX IF NOT EXISTS idx_release_rows_key ON release_rows(table_name, row_key);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | None = None) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def audit(conn, actor: str, action: str, subject: str, detail) -> None:
    from .util import canonical_json, utcnow

    conn.execute(
        "INSERT INTO audit_log(at, actor, action, subject, detail) VALUES (?,?,?,?,?)",
        (utcnow(), actor, action, subject,
         detail if isinstance(detail, str) else canonical_json(detail)),
    )
