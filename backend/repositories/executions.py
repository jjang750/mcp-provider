"""executions + execution_logs CRUD (contract §2 tables, §5.2 ExecutionResult)."""
from __future__ import annotations

import sqlite3
from typing import Optional

from ..models import ExecutionResult, NodeLog
from ._util import dumps, loads, utc_now


def create_execution(
    conn: sqlite3.Connection, workflow_id: int, status: str = "running"
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO executions (workflow_id, status, started_at, finished_at, result)
        VALUES (?, ?, ?, NULL, NULL)
        """,
        (workflow_id, status, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def persist_result(
    conn: sqlite3.Connection, execution_id: int, result: ExecutionResult
) -> None:
    """Write the engine's ExecutionResult (status/result + per-node logs).

    Stores final status/finished_at/result on the executions row and inserts
    one execution_logs row per NodeLog.
    """
    conn.execute(
        """
        UPDATE executions
        SET status = ?, started_at = ?, finished_at = ?, result = ?
        WHERE id = ?
        """,
        (
            result.status,
            result.started_at,
            result.finished_at,
            dumps(result.result) if result.result is not None else None,
            execution_id,
        ),
    )
    # Replace logs (in case of re-persist) then insert.
    conn.execute(
        "DELETE FROM execution_logs WHERE execution_id = ?", (execution_id,)
    )
    for log in result.logs:
        conn.execute(
            """
            INSERT INTO execution_logs (execution_id, node_key, seq, status,
                                        input, output, error, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                log.node_key,
                log.seq,
                log.status,
                dumps(log.input) if log.input is not None else None,
                dumps(log.output) if log.output is not None else None,
                log.error,
                log.timestamp,
            ),
        )
    conn.commit()


def mark_failed(
    conn: sqlite3.Connection, execution_id: int, error: str
) -> None:
    """Mark an execution failed when the engine itself raised (no result)."""
    conn.execute(
        "UPDATE executions SET status = 'failed', finished_at = ? WHERE id = ?",
        (utc_now(), execution_id),
    )
    conn.commit()


def get_execution_result(
    conn: sqlite3.Connection, execution_id: int
) -> Optional[ExecutionResult]:
    r = conn.execute(
        "SELECT * FROM executions WHERE id = ?", (execution_id,)
    ).fetchone()
    if r is None:
        return None
    log_rows = conn.execute(
        "SELECT * FROM execution_logs WHERE execution_id = ? ORDER BY seq, id",
        (execution_id,),
    ).fetchall()
    logs = [
        NodeLog(
            node_key=lr["node_key"],
            seq=lr["seq"],
            status=lr["status"],
            input=loads(lr["input"], None),
            output=loads(lr["output"], None),
            error=lr["error"],
            timestamp=lr["timestamp"],
        )
        for lr in log_rows
    ]
    return ExecutionResult(
        execution_id=r["id"],
        workflow_id=r["workflow_id"],
        status=r["status"],
        started_at=r["started_at"],
        finished_at=r["finished_at"],
        result=loads(r["result"], None),
        logs=logs,
    )
