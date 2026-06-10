"""MCP server entry point (contract §8).

Exposes each ``mcp_exposed=1`` workflow as a single MCP tool:
  * tool name  : ``workflow_{id}_{slug}``  (§8 slug rules)
  * inputSchema: union of *unsatisfied required* params of the start node
                 (params not filled by any edge data_mapping or static params)
  * handler    : ``run_workflow(graph, initial_input=tool_args, auth=<server cfg>)``
                 -> returns ``ExecutionResult.result``

Status: **minimal working stub** (1차 우선순위 낮음, per the agent brief).  The
pure helpers below (``slugify``, ``build_tool_name``, ``build_input_schema``) are
fully implemented and unit-testable.  The DB-backed loading of workflows and the
live ``mcp`` server wiring are guarded so this module **always imports cleanly**
even when the ``mcp`` SDK or the backend repositories are not yet present.

Owner: mcp-engineer.  The backend imports/runs this but does not modify it; it
shares the same SQLite file and the ``engine`` package.

TODO(backend integration):
  * Provide ``load_exposed_workflows()`` and ``make_operation_resolver()`` by
    wiring the backend repositories (workflows/nodes/edges/operations).  The
    expected shapes are documented inline.
  * Confirm the SDK import surface against the pinned ``mcp`` version.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

# engine import works from repo root (engine/ is a sibling of backend/).
try:
    from engine import run_workflow  # noqa: F401
except Exception:  # pragma: no cover - import-path safety in isolation
    run_workflow = None  # type: ignore


# --------------------------------------------------------------------------- #
# Pure helpers (§8) — fully implemented, no external deps
# --------------------------------------------------------------------------- #
def slugify(name: str) -> str:
    """§8 slug: lowercase, non-alnum -> '_', collapse repeats, strip ends."""
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "workflow"


def build_tool_name(workflow_id: int, name: str) -> str:
    """``workflow_{id}_{slug}`` (§8)."""
    return f"workflow_{workflow_id}_{slugify(name)}"


def build_input_schema(
    graph: dict,
    operation_resolver: Optional[Callable[[int], Optional[dict]]] = None,
) -> dict:
    """Derive the MCP tool ``inputSchema`` (JSON Schema object) per §8.

    The required external inputs = required params of the start node (or the
    topologically-first node when there is no start) that are **not** satisfied
    by static params and **not** filled by any incoming edge data_mapping.
    """
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []

    # Pick start node, else first node.
    start = next((n for n in nodes if n.get("type") == "start"), None)
    target_node = start
    if target_node is None or target_node.get("type") == "start":
        # start node has no operation; surface inputs of its downstream api_call.
        downstream_keys = [
            e.get("target") for e in edges
            if start and e.get("source") == start.get("id")
        ]
        nodes_by_key = {n.get("id"): n for n in nodes}
        target_node = next(
            (nodes_by_key.get(k) for k in downstream_keys
             if nodes_by_key.get(k, {}).get("type") == "api_call"),
            None,
        )
    if target_node is None:
        target_node = next((n for n in nodes if n.get("type") == "api_call"), None)
    if target_node is None:
        return {"type": "object", "properties": {}, "required": []}

    operation_id = target_node.get("operation_id")
    op = (
        operation_resolver(operation_id)
        if operation_resolver and operation_id is not None
        else None
    )
    if not op:
        return {"type": "object", "properties": {}, "required": []}

    params_schema = op.get("params_schema") or {}
    static = target_node.get("params") or {}

    # Params filled by incoming edges (their `to` destination keys).
    filled: set[str] = set()
    for e in edges:
        if e.get("target") != target_node.get("id"):
            continue
        for m in e.get("data_mapping") or []:
            to = m.get("to", "")
            parts = to.split(".")
            if len(parts) >= 3:           # params.<bucket>.<key>
                filled.add(f"{parts[1]}.{parts[2]}")

    properties: dict[str, Any] = {}
    required: list[str] = []
    for bucket in ("path", "query", "header"):
        for p in params_schema.get(bucket, []):
            pname = p.get("name")
            if not pname:
                continue
            static_has = pname in (static.get(bucket) or {})
            mapped = f"{bucket}.{pname}" in filled
            if static_has or mapped:
                continue
            prop: dict[str, Any] = {"type": p.get("type", "string")}
            if p.get("description"):
                prop["description"] = p["description"]
            if p.get("enum"):
                prop["enum"] = p["enum"]
            properties[pname] = prop
            if p.get("required"):
                required.append(pname)

    return {"type": "object", "properties": properties, "required": required}


def build_output_schema(
    graph: dict,
    operation_resolver: Optional[Callable[[int], Optional[dict]]] = None,
) -> Optional[dict]:
    """outputSchema = end (or last api_call) node's response_schema['200'] (§8)."""
    nodes = graph.get("nodes") or []
    candidate = next((n for n in nodes if n.get("type") == "end"), None)
    if candidate is None:
        api_nodes = [n for n in nodes if n.get("type") == "api_call"]
        candidate = api_nodes[-1] if api_nodes else None
    if candidate is None or not operation_resolver:
        return None
    operation_id = candidate.get("operation_id")
    if operation_id is None:
        return None
    op = operation_resolver(operation_id)
    if not op:
        return None
    resp = op.get("response_schema") or {}
    return resp.get("200")


# --------------------------------------------------------------------------- #
# DB / server wiring (stub — backend supplies the data source)
# --------------------------------------------------------------------------- #
def load_exposed_workflows() -> list[dict]:
    """Return ``[{"id","name","graph"}]`` for mcp_exposed workflows.

    TODO(backend): wire to workflows/nodes/edges repositories.  ``graph`` must be
    a §4 ``WorkflowGraph`` dict (``workflow_id``, ``nodes``, ``edges``).
    """
    return []


def make_operation_resolver() -> Callable[[int], Optional[dict]]:
    """Return a resolver mapping operations.id -> operation dict (§3 shape).

    TODO(backend): wire to the operations repository.
    """
    def _resolver(operation_id: int) -> Optional[dict]:  # noqa: ARG001
        return None

    return _resolver


def build_tools() -> list[dict]:
    """Build tool descriptors from exposed workflows (transport-agnostic)."""
    resolver = make_operation_resolver()
    tools: list[dict] = []
    for wf in load_exposed_workflows():
        graph = wf.get("graph") or {}
        tools.append(
            {
                "name": build_tool_name(wf["id"], wf.get("name", "")),
                "description": wf.get("description") or wf.get("name", ""),
                "inputSchema": build_input_schema(graph, resolver),
                "outputSchema": build_output_schema(graph, resolver),
                "_graph": graph,
            }
        )
    return tools


def main() -> None:  # pragma: no cover - requires mcp SDK + DB
    """Run the MCP server over stdio (default transport, §8).

    TODO: confirm the import surface against the pinned ``mcp`` version. The
    skeleton below reflects the official ``mcp`` Python SDK low-level server.
    """
    try:
        import asyncio

        import mcp.types as types  # type: ignore
        from mcp.server import Server  # type: ignore
        from mcp.server.stdio import stdio_server  # type: ignore
    except Exception as exc:
        raise SystemExit(
            "MCP SDK not installed. Add 'mcp' to requirements.txt to run the "
            f"MCP server. ({exc})"
        )

    if run_workflow is None:
        raise SystemExit("engine.run_workflow unavailable; cannot start MCP server")

    server = Server("mcp-provider")
    resolver = make_operation_resolver()
    tool_index = {t["name"]: t for t in build_tools()}

    @server.list_tools()
    async def _list_tools() -> list[Any]:
        return [
            types.Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in tool_index.values()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[Any]:
        tool = tool_index.get(name)
        if not tool:
            return [types.TextContent(type="text", text=f"unknown tool: {name}")]
        result = await run_workflow(  # type: ignore[misc]
            tool["_graph"],
            initial_input=arguments,
            auth={},  # TODO: server-side auth configuration
            operation_resolver=resolver,
        )
        if result.get("status") == "failed":
            last_err = next(
                (l.get("error") for l in reversed(result.get("logs", [])) if l.get("error")),
                "execution failed",
            )
            return [types.TextContent(type="text", text=f"FAILED: {last_err}")]
        import json as _json

        return [types.TextContent(type="text", text=_json.dumps(result.get("result")))]

    async def _run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    main()
