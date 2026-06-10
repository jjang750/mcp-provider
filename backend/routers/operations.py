"""/api/operations* endpoints (contract addendum to §5.2).

Single-operation lookup by its DB PK so the frontend can fetch a node's
operation metadata (response/params schemas) using only ``Node.operation_id``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db
from ..models import OperationOut
from ..repositories import specs as specs_repo

router = APIRouter(prefix="/api/operations", tags=["operations"])


@router.get("/{operation_id}", response_model=OperationOut)
def get_operation(operation_id: int, conn=Depends(get_db)) -> OperationOut:
    """Return a single operation's metadata (§5.2 OperationOut) by DB PK.

    404 ErrorResponse if no operation with that id exists.
    """
    op = specs_repo.get_operation(conn, operation_id)
    if op is None:
        raise HTTPException(
            status_code=404, detail=f"Operation {operation_id} not found."
        )
    return op
