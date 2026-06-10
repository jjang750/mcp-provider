"""SQLite connection + idempotent schema migration (contract §2).

Single-file database ``mcp_provider.db`` at the repo root. The DDL is executed
with ``CREATE TABLE IF NOT EXISTS`` so ``init_db`` is safe to re-run.

All JSON columns are stored as ``TEXT`` (``json.dumps``); the repository layer
owns (de)serialization (Assumption 6). PKs are integers; graph-internal
node/edge references use string keys (Assumption 7).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Repo root = parent of this backend/ package directory.
_BACKEND_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_DIR.parent

# Allow override (tests / alt locations) via env var.
DB_PATH = os.environ.get("MCP_PROVIDER_DB", str(_REPO_ROOT / "mcp_provider.db"))


SCHEMA_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS specs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    source_type   TEXT    NOT NULL CHECK (source_type IN ('file', 'url')),
    source_ref    TEXT,
    spec_version  TEXT,
    raw_content   TEXT    NOT NULL,
    parsed_at     TEXT,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_id         INTEGER NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
    operation_id    TEXT    NOT NULL,
    method          TEXT    NOT NULL,
    path            TEXT    NOT NULL,
    base_url        TEXT,
    summary         TEXT,
    params_schema   TEXT    NOT NULL DEFAULT '{}',
    request_schema  TEXT,
    response_schema TEXT,
    auth            TEXT,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operations_spec ON operations(spec_id);

CREATE TABLE IF NOT EXISTS workflows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    description  TEXT,
    mcp_exposed  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id   INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    node_key      TEXT    NOT NULL,
    operation_id  INTEGER REFERENCES operations(id) ON DELETE SET NULL,
    type          TEXT    NOT NULL,
    label         TEXT    NOT NULL DEFAULT '',
    params        TEXT    NOT NULL DEFAULT '{}',
    position_x    REAL    NOT NULL DEFAULT 0,
    position_y    REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_nodes_workflow ON nodes(workflow_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_nodes_wf_key ON nodes(workflow_id, node_key);

CREATE TABLE IF NOT EXISTS edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id     INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    edge_key        TEXT    NOT NULL,
    source_node_key TEXT    NOT NULL,
    target_node_key TEXT    NOT NULL,
    data_mapping    TEXT    NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_edges_workflow ON edges(workflow_id);

CREATE TABLE IF NOT EXISTS executions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id  INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    status       TEXT    NOT NULL,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT,
    result       TEXT
);
CREATE INDEX IF NOT EXISTS idx_executions_workflow ON executions(workflow_id);

CREATE TABLE IF NOT EXISTS execution_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id  INTEGER NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    node_key      TEXT    NOT NULL,
    seq           INTEGER NOT NULL,
    status        TEXT    NOT NULL,
    input         TEXT,
    output        TEXT,
    error         TEXT,
    timestamp     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_execution ON execution_logs(execution_id);
"""


def get_connection() -> sqlite3.Connection:
    """Open a new SQLite connection with sane defaults for FastAPI.

    ``check_same_thread=False`` because FastAPI may dispatch handlers across
    threads (sync def in a threadpool). Each request opens its own short-lived
    connection via the ``get_db`` dependency, so cross-thread sharing of a
    single connection object is avoided in practice.
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    """Create all tables/indexes if they do not exist (idempotent)."""
    conn = get_connection()
    try:
        conn.executescript(SCHEMA_DDL)
        conn.commit()
    finally:
        conn.close()


def get_db():
    """FastAPI dependency: yields a connection, always closed afterwards."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()
