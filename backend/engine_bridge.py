"""Thin bridge to the mcp-engineer-owned ``engine`` package (contract §6/§9).

The backend only **imports and calls** the engine; it never implements parsing
or execution. Per §6 the engine exposes::

    from engine import run_workflow, parse_openapi, ParseResult

To keep the FastAPI app importable/bootable while the engine is still being
built (it may not yet export these symbols), we resolve them lazily and raise a
clear HTTP-friendly error at call time rather than crashing at import time.
Runtime coupling is verified by QA.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from .models import ExecutionResult, WorkflowGraph


class EngineUnavailableError(RuntimeError):
    """Raised when an engine entry point is missing/not yet implemented."""


def _resolve(name: str):
    try:
        import engine  # noqa: WPS433 (intentional lazy import)
    except Exception as exc:  # engine package not importable yet
        raise EngineUnavailableError(
            f"engine package not importable: {exc!r}"
        ) from exc
    fn = getattr(engine, name, None)
    if fn is None:
        raise EngineUnavailableError(
            f"engine.{name} is not yet implemented (contract §6)."
        )
    return fn


def parse_openapi(raw_content: str, source_hint: Optional[str] = None):
    """Call ``engine.parse_openapi`` (§6). Returns a ParseResult-like object.

    Expected return attributes/keys: ``spec_version``, ``base_url``,
    ``operations`` (list[dict]), ``warnings`` (list[str]).
    """
    fn = _resolve("parse_openapi")
    return fn(raw_content, source_hint)


async def run_workflow(
    graph: WorkflowGraph,
    initial_input: Optional[dict] = None,
    auth: Optional[dict] = None,
    on_node_event: Optional[Callable[[Any], Awaitable[None] | None]] = None,
    operation_resolver: Optional[Callable[[int], Optional[dict]]] = None,
) -> ExecutionResult:
    """Call ``engine.run_workflow`` (§6). Awaits the coroutine.

    ``operation_resolver`` is a callback ``(operation_id: int) -> dict | None``
    that the engine uses to fetch each ``api_call`` node's operation definition
    (method/path/base_url/auth/request_schema...). It MUST be provided for
    api_call nodes to execute; the engine fails such nodes otherwise. It is
    forwarded to the engine's keyword-only ``operation_resolver`` parameter.

    Returns something matching the §5.0 ``ExecutionResult`` shape; we coerce it
    into the canonical model so persistence/serialization is uniform.
    """
    fn = _resolve("run_workflow")
    result = await fn(
        graph,
        initial_input=initial_input,
        auth=auth,
        on_node_event=on_node_event,
        operation_resolver=operation_resolver,
    )
    return _coerce_execution_result(result)


def _coerce_execution_result(result: Any) -> ExecutionResult:
    """Accept an ExecutionResult, a compatible model, or a dict."""
    if isinstance(result, ExecutionResult):
        return result
    if hasattr(result, "model_dump"):
        return ExecutionResult.model_validate(result.model_dump())
    if isinstance(result, dict):
        return ExecutionResult.model_validate(result)
    raise EngineUnavailableError(
        f"engine.run_workflow returned unsupported type: {type(result)!r}"
    )
