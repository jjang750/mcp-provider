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

import copy
import os
import re
from typing import Any, Callable, Optional

# Optional group filter so one workflow DB can be split across multiple MCP
# servers. Set ``MCP_GROUP=xperp`` (per connector in claude_desktop_config) and
# this process only exposes workflows whose ``mcp_group`` matches. Empty/None
# exposes all exposed workflows (single combined server). ``MCP_SERVER_NAME``
# overrides the advertised server name.
MCP_GROUP = (os.environ.get("MCP_GROUP") or "").strip() or None
MCP_SERVER_NAME = (
    os.environ.get("MCP_SERVER_NAME")
    or (f"mcp-{MCP_GROUP}" if MCP_GROUP else "mcp-provider")
)

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


def sanitize_tool_name(name: str) -> str:
    """Reduce a user-supplied tool name to MCP-safe chars ``[A-Za-z0-9_-]``."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip())
    s = re.sub(r"_+", "_", s).strip("_-")
    return s


def build_tool_name(
    workflow_id: int, name: str, override: Optional[str] = None
) -> str:
    """Tool name. Uses the user-supplied ``override`` verbatim (sanitized) when
    set, so a workflow can be named e.g. ``xperp_charge_detail`` and be obvious
    at a glance. Falls back to ``workflow_{id}_{slug}`` (§8) otherwise."""
    if override:
        cleaned = sanitize_tool_name(override)
        if cleaned:
            return cleaned
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
    """Return ``[{"id","name","description","graph"}]`` for mcp_exposed workflows.

    Wired to the workflows repository. ``graph`` is a §4 ``WorkflowGraph`` dict
    (``workflow_id``, ``nodes``, ``edges``) produced via ``model_dump(by_alias=True)``
    so the ``data_mapping`` wire key ``from`` is preserved for the engine.
    """
    from backend.db import get_connection
    from backend.repositories import workflows as wf_repo

    conn = get_connection()
    try:
        out: list[dict] = []
        for summary in wf_repo.list_workflows(conn):
            if not summary.mcp_exposed:
                continue
            # When MCP_GROUP is set, only expose workflows in that group.
            if MCP_GROUP is not None and (summary.mcp_group or None) != MCP_GROUP:
                continue
            graph = wf_repo.load_graph(conn, summary.id)
            if graph is None:
                continue
            out.append(
                {
                    "id": summary.id,
                    "name": summary.name,
                    "description": summary.description,
                    "tool_name": summary.mcp_tool_name,
                    "graph": graph.model_dump(by_alias=True),
                }
            )
        return out
    finally:
        conn.close()


def make_operation_resolver() -> Callable[[int], Optional[dict]]:
    """Return a resolver mapping operations.id -> operation dict (§3 shape).

    Wired to the operations repository. A short-lived connection is opened per
    call (operations are read-only) so the resolver is safe across threads.
    """
    from backend.db import get_connection
    from backend.repositories import specs as specs_repo

    def _resolver(operation_id: int) -> Optional[dict]:
        if operation_id is None:
            return None
        conn = get_connection()
        try:
            op = specs_repo.get_operation(conn, operation_id)
            return op.model_dump() if op is not None else None
        finally:
            conn.close()

    return _resolver


def apply_tool_args(
    graph: dict,
    arguments: dict,
    operation_resolver: Optional[Callable[[int], Optional[dict]]] = None,
) -> dict:
    """Inject MCP tool ``arguments`` into the target node's static params (§8).

    ``build_input_schema`` advertises a tool's inputs as the unsatisfied required
    params of the start-connected api_call node, but the engine only injects
    ``initial_input`` through edge ``data_mapping``. So we route each argument
    into the matching node param (path/query/header by name, or requestBody key)
    on a *deep copy* of the graph so concurrent calls don't clobber each other.
    """
    g = copy.deepcopy(graph)
    if not arguments:
        return g
    nodes = g.get("nodes") or []
    edges = g.get("edges") or []
    start = next((n for n in nodes if n.get("type") == "start"), None)
    nodes_by_key = {n.get("id"): n for n in nodes}

    targets: list[dict] = []
    if start is not None:
        for e in edges:
            if e.get("source") == start.get("id"):
                t = nodes_by_key.get(e.get("target"))
                if t and t.get("type") == "api_call":
                    targets.append(t)
    if not targets:
        targets = [n for n in nodes if n.get("type") == "api_call"]

    for node in targets:
        op_id = node.get("operation_id")
        op = operation_resolver(op_id) if (operation_resolver and op_id is not None) else None
        if not op:
            continue
        params_schema = op.get("params_schema") or {}
        params = node.setdefault("params", {})
        for bucket in ("path", "query", "header"):
            for p in params_schema.get(bucket, []):
                name = p.get("name")
                if name and name in arguments:
                    params.setdefault(bucket, {})[name] = arguments[name]
        rs = op.get("request_schema")
        if rs:
            props = (rs.get("schema") or {}).get("properties") or {}
            for key in props:
                if key in arguments:
                    body = params.get("body")
                    if not isinstance(body, dict):
                        body = {}
                        params["body"] = body
                    body[key] = arguments[key]
    return g


def build_tools() -> list[dict]:
    """Build tool descriptors from exposed workflows (transport-agnostic)."""
    resolver = make_operation_resolver()
    tools: list[dict] = []
    for wf in load_exposed_workflows():
        graph = wf.get("graph") or {}
        tools.append(
            {
                "name": build_tool_name(
                    wf["id"], wf.get("name", ""), wf.get("tool_name")
                ),
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

    server = Server(MCP_SERVER_NAME)
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
        # Route tool args into the target node params (engine injects initial_input
        # only via edge mappings), then also pass them as initial_input so any
        # mapping-based flows still work.
        graph = apply_tool_args(tool["_graph"], arguments or {}, resolver)
        result = await run_workflow(  # type: ignore[misc]
            graph,
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
