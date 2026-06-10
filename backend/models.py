"""Canonical Pydantic v2 models for mcp-provider.

This module is the **single source of truth** for the shared wire models
(``Node`` / ``Edge`` / ``WorkflowGraph`` / ``ExecutionResult``) per contract §9.
Keys and shapes follow ``_workspace/01_architect_contracts.md`` §5.0 / §5.2 exactly.

Do not change key names or nesting without an architect contract update — the
engine and frontend depend on these wire shapes (§11).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 5.0 Common / shared graph models
# ---------------------------------------------------------------------------

NodeType = Literal["api_call", "start", "end", "transform"]


class Position(BaseModel):
    x: float = 0
    y: float = 0


class NodeParams(BaseModel):
    path: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    header: dict[str, Any] = Field(default_factory=dict)
    body: Optional[Any] = None


class Node(BaseModel):
    id: str
    type: NodeType
    label: str = ""
    operation_id: Optional[int] = None
    params: NodeParams = Field(default_factory=NodeParams)
    position: Position = Field(default_factory=Position)


class DataMappingItem(BaseModel):
    # Wire key is "from"; ``from_`` is only the Python-side attribute name.
    from_: str = Field(alias="from")
    to: str
    model_config = {"populate_by_name": True}


class Edge(BaseModel):
    id: str
    source: str
    target: str
    data_mapping: list[DataMappingItem] = Field(default_factory=list)


class WorkflowGraph(BaseModel):
    workflow_id: int
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    detail: str


# ---------------------------------------------------------------------------
# 5.2 specs
# ---------------------------------------------------------------------------


class SpecSummary(BaseModel):
    id: int
    name: str
    source_type: Literal["file", "url"]
    spec_version: Optional[str] = None
    created_at: str


class OperationOut(BaseModel):
    id: int
    spec_id: int
    operation_id: str
    method: str
    path: str
    base_url: Optional[str] = None
    summary: Optional[str] = None
    params_schema: dict[str, Any] = Field(default_factory=dict)
    request_schema: Optional[dict[str, Any]] = None
    response_schema: Optional[dict[str, Any]] = None
    auth: Optional[dict[str, Any]] = None


class SpecUploadResult(BaseModel):
    spec: SpecSummary
    operation_count: int
    operations: list[OperationOut]
    warnings: list[str] = Field(default_factory=list)


class SpecFromUrlRequest(BaseModel):
    url: str
    name: Optional[str] = None


# ---------------------------------------------------------------------------
# 5.2 workflows
# ---------------------------------------------------------------------------


class WorkflowSummary(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    mcp_exposed: bool
    created_at: str
    updated_at: str


class WorkflowCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None


class WorkflowDetail(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    mcp_exposed: bool
    created_at: str
    updated_at: str
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)


class WorkflowSaveRequest(BaseModel):
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    name: Optional[str] = None
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# 5.2 run / executions
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    initial_input: dict[str, Any] = Field(default_factory=dict)
    auth: dict[str, Any] = Field(default_factory=dict)


class NodeLog(BaseModel):
    node_key: str
    seq: int
    status: Literal["success", "failed", "skipped"]
    input: Optional[dict[str, Any]] = None
    output: Optional[Any] = None
    error: Optional[str] = None
    timestamp: str


class ExecutionResult(BaseModel):
    execution_id: int
    workflow_id: int
    status: Literal["running", "success", "failed"]
    started_at: str
    finished_at: Optional[str] = None
    result: Optional[Any] = None
    logs: list[NodeLog] = Field(default_factory=list)


__all__ = [
    "NodeType",
    "Position",
    "NodeParams",
    "Node",
    "DataMappingItem",
    "Edge",
    "WorkflowGraph",
    "ErrorResponse",
    "SpecSummary",
    "OperationOut",
    "SpecUploadResult",
    "SpecFromUrlRequest",
    "WorkflowSummary",
    "WorkflowCreateRequest",
    "WorkflowDetail",
    "WorkflowSaveRequest",
    "RunRequest",
    "NodeLog",
    "ExecutionResult",
]
