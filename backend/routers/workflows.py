"""/api/workflows* endpoints incl. /run (contract §5.1 / §5.2 / §6)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import engine_bridge
from ..db import get_db
from ..models import (
    ExecutionResult,
    RunRequest,
    WorkflowCreateRequest,
    WorkflowDetail,
    WorkflowSaveRequest,
    WorkflowSummary,
)
from ..repositories import executions as exec_repo
from ..repositories import specs as specs_repo
from ..repositories import workflows as wf_repo

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


@router.get("", response_model=list[WorkflowSummary])
def list_workflows(conn=Depends(get_db)) -> list[WorkflowSummary]:
    return wf_repo.list_workflows(conn)


@router.post("", response_model=WorkflowDetail)
def create_workflow(
    body: WorkflowCreateRequest, conn=Depends(get_db)
) -> WorkflowDetail:
    wf_id = wf_repo.create_workflow(
        conn, name=body.name, description=body.description
    )
    detail = wf_repo.get_workflow_detail(conn, wf_id)
    if detail is None:  # pragma: no cover - just created
        raise HTTPException(status_code=500, detail="Workflow creation failed.")
    return detail


@router.get("/{wf_id}", response_model=WorkflowDetail)
def get_workflow(wf_id: int, conn=Depends(get_db)) -> WorkflowDetail:
    detail = wf_repo.get_workflow_detail(conn, wf_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Workflow {wf_id} not found.")
    return detail


@router.put("/{wf_id}", response_model=WorkflowDetail)
def save_workflow(
    wf_id: int, body: WorkflowSaveRequest, conn=Depends(get_db)
) -> WorkflowDetail:
    if wf_repo.get_workflow_row(conn, wf_id) is None:
        raise HTTPException(status_code=404, detail=f"Workflow {wf_id} not found.")
    wf_repo.replace_graph(
        conn,
        wf_id,
        body.nodes,
        body.edges,
        name=body.name,
        description=body.description,
    )
    return wf_repo.get_workflow_detail(conn, wf_id)


@router.delete("/{wf_id}")
def delete_workflow(wf_id: int, conn=Depends(get_db)) -> dict:
    if not wf_repo.delete_workflow(conn, wf_id):
        raise HTTPException(status_code=404, detail=f"Workflow {wf_id} not found.")
    return {"deleted": True}


@router.post("/{wf_id}/run", response_model=ExecutionResult)
async def run_workflow(
    wf_id: int, body: RunRequest, conn=Depends(get_db)
) -> ExecutionResult:
    graph = wf_repo.load_graph(conn, wf_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Workflow {wf_id} not found.")

    execution_id = exec_repo.create_execution(conn, wf_id, status="running")

    # Resolver callback the engine uses to fetch each api_call node's operation
    # definition by its operations.id (int PK). Defined here so it closes over
    # the live `conn` for the duration of the (awaited) run (§6, BUG-1 fix).
    def _operation_resolver(op_id: int):
        op = specs_repo.get_operation(conn, op_id)
        return op.model_dump() if op is not None else None

    try:
        result = await engine_bridge.run_workflow(
            graph,
            initial_input=body.initial_input,
            auth=body.auth,
            operation_resolver=_operation_resolver,
        )
    except engine_bridge.EngineUnavailableError as exc:
        exec_repo.mark_failed(conn, execution_id, str(exc))
        raise HTTPException(
            status_code=503, detail=f"Execution engine unavailable: {exc}"
        ) from exc
    except Exception as exc:  # engine itself blew up (not a node failure)
        exec_repo.mark_failed(conn, execution_id, str(exc))
        raise HTTPException(
            status_code=500, detail=f"Execution failed: {exc}"
        ) from exc

    # Stamp our DB ids onto the engine result, then persist.
    result.execution_id = execution_id
    result.workflow_id = wf_id
    exec_repo.persist_result(conn, execution_id, result)
    # Note: node failures are NOT HTTP errors — returned as 200 + status:"failed".
    return result
