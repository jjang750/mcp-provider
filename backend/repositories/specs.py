"""specs + operations CRUD (contract §2 tables, §5.2 shapes)."""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from ..models import OperationOut, SpecSummary
from ._util import dumps, loads, utc_now


# --------------------------------------------------------------------------
# specs
# --------------------------------------------------------------------------


def create_spec(
    conn: sqlite3.Connection,
    *,
    name: str,
    source_type: str,
    source_ref: Optional[str],
    spec_version: Optional[str],
    raw_content: str,
    parsed_at: Optional[str],
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO specs (name, source_type, source_ref, spec_version,
                           raw_content, parsed_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (name, source_type, source_ref, spec_version, raw_content, parsed_at, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_spec_row(conn: sqlite3.Connection, spec_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM specs WHERE id = ?", (spec_id,)).fetchone()


def list_specs(conn: sqlite3.Connection) -> list[SpecSummary]:
    rows = conn.execute(
        "SELECT id, name, source_type, spec_version, created_at "
        "FROM specs ORDER BY id DESC"
    ).fetchall()
    return [
        SpecSummary(
            id=r["id"],
            name=r["name"],
            source_type=r["source_type"],
            spec_version=r["spec_version"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def spec_summary(conn: sqlite3.Connection, spec_id: int) -> Optional[SpecSummary]:
    r = get_spec_row(conn, spec_id)
    if r is None:
        return None
    return SpecSummary(
        id=r["id"],
        name=r["name"],
        source_type=r["source_type"],
        spec_version=r["spec_version"],
        created_at=r["created_at"],
    )


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------


def insert_operations(
    conn: sqlite3.Connection, spec_id: int, operations: list[dict[str, Any]]
) -> int:
    """Insert parser-produced operation dicts. Returns count inserted.

    Each ``op`` dict uses the §3/§5.2 keys (the DB-independent subset from
    ParseResult.operations): operation_id, method, path, base_url, summary,
    params_schema, request_schema, response_schema, auth.
    """
    now = utc_now()
    count = 0
    for op in operations:
        conn.execute(
            """
            INSERT INTO operations (spec_id, operation_id, method, path, base_url,
                                    summary, params_schema, request_schema,
                                    response_schema, auth, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                spec_id,
                str(op.get("operation_id") or ""),
                str(op.get("method") or "").upper(),
                str(op.get("path") or ""),
                op.get("base_url"),
                op.get("summary"),
                dumps(op.get("params_schema") or {}),
                dumps(op["request_schema"]) if op.get("request_schema") is not None else None,
                dumps(op["response_schema"]) if op.get("response_schema") is not None else None,
                dumps(op["auth"]) if op.get("auth") is not None else None,
                now,
            ),
        )
        count += 1
    conn.commit()
    return count


def _row_to_operation(r: sqlite3.Row) -> OperationOut:
    return OperationOut(
        id=r["id"],
        spec_id=r["spec_id"],
        operation_id=r["operation_id"],
        method=r["method"],
        path=r["path"],
        base_url=r["base_url"],
        summary=r["summary"],
        params_schema=loads(r["params_schema"], {}) or {},
        request_schema=loads(r["request_schema"], None),
        response_schema=loads(r["response_schema"], None),
        auth=loads(r["auth"], None),
    )


def list_operations_for_spec(
    conn: sqlite3.Connection, spec_id: int
) -> list[OperationOut]:
    rows = conn.execute(
        "SELECT * FROM operations WHERE spec_id = ? ORDER BY id", (spec_id,)
    ).fetchall()
    return [_row_to_operation(r) for r in rows]


def get_operation(conn: sqlite3.Connection, operation_pk: int) -> Optional[OperationOut]:
    r = conn.execute(
        "SELECT * FROM operations WHERE id = ?", (operation_pk,)
    ).fetchone()
    return _row_to_operation(r) if r is not None else None
