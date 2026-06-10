"""/api/executions* endpoints (contract §5.1 / §5.2).

GET /api/executions/{exec_id} -> ExecutionResult (status + logs).
SSE stream (§5.1 optional) is intentionally NOT implemented in the 1st MVP;
clients poll this endpoint instead (Assumption 2).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db
from ..models import ExecutionResult
from ..repositories import executions as exec_repo

router = APIRouter(prefix="/api/executions", tags=["executions"])


@router.get("/{exec_id}", response_model=ExecutionResult)
def get_execution(exec_id: int, conn=Depends(get_db)) -> ExecutionResult:
    result = exec_repo.get_execution_result(conn, exec_id)
    if result is None:
        raise HTTPException(
            status_code=404, detail=f"Execution {exec_id} not found."
        )
    return result
