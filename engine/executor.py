"""Workflow execution engine (contract §6, §4, §5.0).

``run_workflow`` takes a graph (``WorkflowGraph`` model **or** an equivalent
dict with the same keys, per §6), validates it (rejecting cycles), topologically
sorts the nodes, executes them sequentially, applies each incoming edge's
``data_mapping`` to inject upstream output into the current node's input, makes
the real HTTP call for ``api_call`` nodes, and records a log per node.

Return shape == §5.0 ``ExecutionResult`` as a plain ``dict`` with keys:
``execution_id, workflow_id, status, started_at, finished_at, result, logs`` and
each log == §5.0 ``NodeLog`` (``node_key, seq, status, input, output, error,
timestamp``).

DB-assigned fields the engine cannot know are placeholders for the backend to
overwrite: ``execution_id = 0``.  Per §5.0/§11, node failure does **not** raise —
it yields ``status="failed"`` with logs preserved (the engine never dies).

Pure module — no FastAPI dependency.  Operation metadata (method/path/base_url/
auth/...) needed for ``api_call`` nodes is resolved via an injected
``operation_resolver(operation_id:int) -> dict | None``.  The backend supplies
this (looking up the ``operations`` table); without it, ``api_call`` nodes whose
operation cannot be resolved fail gracefully.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Union

from . import http_client

# A graph may arrive as a Pydantic model or a dict with identical keys (§6).
GraphLike = Union[dict, Any]
OperationResolver = Callable[[int], Optional[dict]]
NodeEventCb = Callable[[dict], Union[None, Awaitable[None]]]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Graph normalisation (accept model or dict)
# --------------------------------------------------------------------------- #
def _as_dict(obj: Any) -> Any:
    """Coerce a Pydantic model (v2/v1) to a plain dict with wire keys (by alias)."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):  # pydantic v2
        try:
            return obj.model_dump(by_alias=True)
        except TypeError:
            return obj.model_dump()
    if hasattr(obj, "dict"):  # pydantic v1
        try:
            return obj.dict(by_alias=True)
        except TypeError:
            return obj.dict()
    return obj


def _normalise_graph(graph: GraphLike) -> dict:
    g = _as_dict(graph)
    if not isinstance(g, dict):
        raise ValueError("graph must be a WorkflowGraph model or dict")
    nodes = [_as_dict(n) for n in (g.get("nodes") or [])]
    edges = [_as_dict(e) for e in (g.get("edges") or [])]
    return {
        "workflow_id": g.get("workflow_id"),
        "nodes": nodes,
        "edges": edges,
    }


def _mapping_from(item: dict) -> str:
    """Read the source path, tolerating the pydantic alias ``from_`` (§11)."""
    if "from" in item:
        return item["from"]
    return item.get("from_", "")


# --------------------------------------------------------------------------- #
# JSONPath subset resolution (contract §4: $, dotted, [i])
# --------------------------------------------------------------------------- #
def resolve_path(source_output: Any, path: str) -> tuple[bool, Any]:
    """Resolve a contract-subset JSONPath against a node's output.

    Supports: root ``$``, dotted access (``$.a.b``), list index (``$.a[0].b``),
    with or without a leading ``$``/``$.``.  Returns ``(found, value)``.
    """
    if path is None:
        return False, None
    expr = path.strip()
    if expr in ("$", ""):
        return True, source_output
    if expr.startswith("$"):
        expr = expr[1:]
    expr = expr.lstrip(".")

    tokens: list[Union[str, int]] = []
    for segment in expr.split("."):
        if not segment:
            continue
        # split out trailing [i][j] indices
        m = segment.split("[")
        key = m[0]
        if key:
            tokens.append(key)
        for idx_part in m[1:]:
            idx_str = idx_part.rstrip("]")
            try:
                tokens.append(int(idx_str))
            except ValueError:
                return False, None  # unsupported (filter/wildcard)

    cur = source_output
    for tok in tokens:
        if isinstance(tok, int):
            if isinstance(cur, (list, tuple)) and -len(cur) <= tok < len(cur):
                cur = cur[tok]
            else:
                return False, None
        else:
            if isinstance(cur, dict) and tok in cur:
                cur = cur[tok]
            else:
                return False, None
    return True, cur


def _set_input_path(input_obj: dict, to_path: str, value: Any) -> None:
    """Apply a ``to`` destination path (always starts with ``params.``, §4)."""
    parts = to_path.strip().split(".")
    if not parts or parts[0] != "params":
        # Be lenient: still try to set under the raw path.
        parts = ["params"] + parts if parts[0] != "params" else parts

    params = input_obj.setdefault("params", {})

    # params.body  (whole body) special-case
    if len(parts) == 2 and parts[1] == "body":
        params["body"] = value
        return
    # params.body.<k>...  -> nested into body dict
    if len(parts) >= 3 and parts[1] == "body":
        container = params.setdefault("body", {})
        if not isinstance(container, dict):
            container = {}
            params["body"] = container
        _assign_nested(container, parts[2:], value)
        return
    # params.path|query|header.<k>
    if len(parts) >= 3 and parts[1] in ("path", "query", "header"):
        bucket = params.setdefault(parts[1], {})
        if not isinstance(bucket, dict):
            bucket = {}
            params[parts[1]] = bucket
        _assign_nested(bucket, parts[2:], value)
        return
    # fallback: nest under params
    _assign_nested(params, parts[1:], value)


def _assign_nested(container: dict, keys: list[str], value: Any) -> None:
    cur = container
    for k in keys[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    if keys:
        cur[keys[-1]] = value


# --------------------------------------------------------------------------- #
# Topological ordering + cycle detection
# --------------------------------------------------------------------------- #
class CycleError(Exception):
    pass


def _topo_sort(nodes: list[dict], edges: list[dict]) -> tuple[list[str], list[str]]:
    """Kahn's algorithm. Returns (ordered_node_keys, warnings). Raises on cycle."""
    node_keys = [n["id"] for n in nodes]
    key_set = set(node_keys)
    warnings: list[str] = []

    indeg: dict[str, int] = {k: 0 for k in node_keys}
    adj: dict[str, list[str]] = {k: [] for k in node_keys}

    for e in edges:
        src, tgt = e.get("source"), e.get("target")
        if src not in key_set or tgt not in key_set:
            warnings.append(
                f"edge '{e.get('id')}' references unknown node(s) "
                f"({src} -> {tgt}); ignored"
            )
            continue
        adj[src].append(tgt)
        indeg[tgt] += 1

    # Start nodes first (stable, deterministic) then by original order.
    def _sort_key(k: str) -> tuple:
        node = next((n for n in nodes if n["id"] == k), {})
        is_start = 0 if node.get("type") == "start" else 1
        return (is_start, node_keys.index(k))

    queue = sorted([k for k, d in indeg.items() if d == 0], key=_sort_key)
    ordered: list[str] = []
    while queue:
        cur = queue.pop(0)
        ordered.append(cur)
        next_ready = []
        for nb in adj[cur]:
            indeg[nb] -= 1
            if indeg[nb] == 0:
                next_ready.append(nb)
        queue = sorted(queue + next_ready, key=_sort_key)

    if len(ordered) != len(node_keys):
        unresolved = [k for k in node_keys if k not in ordered]
        raise CycleError(
            f"cycle detected in workflow graph; involved nodes: {unresolved}"
        )

    # Isolated node warning (no edges at all touching it, and not start/end).
    touched = set()
    for e in edges:
        touched.add(e.get("source"))
        touched.add(e.get("target"))
    for n in nodes:
        if n["id"] not in touched and n.get("type") not in ("start", "end"):
            warnings.append(f"node '{n['id']}' is isolated (no edges)")

    return ordered, warnings


# --------------------------------------------------------------------------- #
# Per-node input assembly
# --------------------------------------------------------------------------- #
def _default_params(node: dict) -> dict:
    raw = node.get("params") or {}
    raw = _as_dict(raw)
    return {
        "path": dict(raw.get("path") or {}),
        "query": dict(raw.get("query") or {}),
        "header": dict(raw.get("header") or {}),
        "body": raw.get("body", None),
    }


def _assemble_input(
    node: dict,
    incoming_edges: list[dict],
    outputs: dict[str, Any],
) -> tuple[dict, list[str]]:
    """Build node input: static params + incoming edge data_mapping overrides."""
    node_input: dict[str, Any] = {"params": _default_params(node)}
    map_warnings: list[str] = []

    for edge in incoming_edges:
        src = edge.get("source")
        src_output = outputs.get(src)
        for item in edge.get("data_mapping") or []:
            item = _as_dict(item)
            from_path = _mapping_from(item)
            to_path = item.get("to", "")
            found, value = resolve_path(src_output, from_path)
            if not found:
                map_warnings.append(
                    f"mapping '{from_path}' -> '{to_path}' on edge "
                    f"'{edge.get('id')}': source path not found; skipped"
                )
                continue
            if not to_path:
                map_warnings.append(
                    f"mapping on edge '{edge.get('id')}' missing 'to'; skipped"
                )
                continue
            _set_input_path(node_input, to_path, value)

    return node_input, map_warnings


# --------------------------------------------------------------------------- #
# Node execution
# --------------------------------------------------------------------------- #
async def _emit(on_node_event: Optional[NodeEventCb], log: dict) -> None:
    if on_node_event is None:
        return
    try:
        res = on_node_event(log)
        if inspect.isawaitable(res):
            await res
    except Exception:  # noqa: BLE001 - callback errors must not break execution
        pass


async def _execute_api_call(
    node: dict,
    node_input: dict,
    operation_resolver: Optional[OperationResolver],
    auth_values: Optional[dict],
    timeout: float,
) -> tuple[Any, Optional[str]]:
    """Returns (output, error). error None on success."""
    operation_id = node.get("operation_id")
    if operation_id is None:
        return None, "api_call node has no operation_id"
    if operation_resolver is None:
        return None, "no operation_resolver provided to engine"

    op = operation_resolver(operation_id)
    if not op:
        return None, f"operation_id {operation_id} not found"

    params = node_input.get("params", {})
    request_schema = op.get("request_schema") or {}
    content_type = "application/json"
    if isinstance(request_schema, dict):
        content_type = request_schema.get("content_type", "application/json")

    try:
        result = await http_client.call(
            method=op.get("method", "GET"),
            base_url=op.get("base_url"),
            path=op.get("path", ""),
            path_params=params.get("path") or {},
            query=params.get("query") or {},
            headers=params.get("header") or {},
            body=params.get("body"),
            body_content_type=content_type,
            auth_meta=op.get("auth"),
            auth_values=auth_values,
            timeout=timeout,
        )
    except http_client.HttpCallError as exc:
        return None, str(exc)

    if result.is_error:
        return result.json, f"HTTP {result.status_code}"
    return result.json, None


async def _execute_transform(node: dict, node_input: dict) -> tuple[Any, Optional[str]]:
    """v1 transform: pass-through of assembled body/params (no code exec)."""
    params = node_input.get("params", {})
    body = params.get("body")
    if body is not None:
        return body, None
    # Otherwise expose the merged params so downstream mapping can read them.
    return params, None


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
async def run_workflow(
    graph: GraphLike,
    initial_input: Optional[dict] = None,
    auth: Optional[dict] = None,
    on_node_event: Optional[NodeEventCb] = None,
    *,
    operation_resolver: Optional[OperationResolver] = None,
    timeout: float = http_client.DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Execute a workflow graph sequentially. See module docstring / contract §6.

    ``initial_input`` is exposed as the ``start`` node's output, becoming the
    ``$`` reference point for edges leaving the start node (§7).  ``auth`` carries
    runtime secrets ``{"token":..., "api_key":...}`` injected per §0.8 — never
    persisted.  ``operation_resolver`` (backend-supplied) maps an
    ``operation_id`` (int, DB PK) to its operation metadata dict.

    Returns an ``ExecutionResult``-shaped dict (§5.0) with ``execution_id=0`` as a
    placeholder for the backend to overwrite.  Never raises on node failure.
    """
    initial_input = initial_input or {}
    auth = auth or {}
    started_at = _utcnow()

    g = _normalise_graph(graph)
    workflow_id = g.get("workflow_id") or 0
    nodes = g["nodes"]
    edges = g["edges"]

    logs: list[dict] = []

    # --- validation / ordering ------------------------------------------------
    try:
        ordered_keys, topo_warnings = _topo_sort(nodes, edges)
    except CycleError as exc:
        return {
            "execution_id": 0,
            "workflow_id": workflow_id,
            "status": "failed",
            "started_at": started_at,
            "finished_at": _utcnow(),
            "result": None,
            "logs": [
                {
                    "node_key": "__graph__",
                    "seq": 0,
                    "status": "failed",
                    "input": None,
                    "output": None,
                    "error": str(exc),
                    "timestamp": _utcnow(),
                }
            ],
        }

    nodes_by_key = {n["id"]: n for n in nodes}
    incoming: dict[str, list[dict]] = {k: [] for k in nodes_by_key}
    for e in edges:
        tgt = e.get("target")
        if tgt in incoming:
            incoming[tgt].append(e)

    outputs: dict[str, Any] = {}
    aborted = False
    final_output: Any = None
    seq = 0

    for key in ordered_keys:
        node = nodes_by_key[key]
        ntype = node.get("type")

        if aborted:
            log = {
                "node_key": key, "seq": seq, "status": "skipped",
                "input": None, "output": None,
                "error": "skipped due to upstream failure",
                "timestamp": _utcnow(),
            }
            logs.append(log)
            await _emit(on_node_event, log)
            seq += 1
            continue

        # --- assemble input ---------------------------------------------------
        node_input, map_warnings = _assemble_input(node, incoming[key], outputs)

        # --- execute by type --------------------------------------------------
        error: Optional[str] = None
        output: Any = None

        if ntype == "start":
            # start node output = external initial_input (§7).
            output = initial_input
        elif ntype == "end":
            # end node surfaces its assembled body (or the single upstream output).
            params_body = node_input.get("params", {}).get("body")
            if params_body is not None:
                output = params_body
            else:
                upstream = [e.get("source") for e in incoming[key]]
                output = outputs.get(upstream[0]) if upstream else None
        elif ntype == "transform":
            output, error = await _execute_transform(node, node_input)
        elif ntype == "api_call":
            output, error = await _execute_api_call(
                node, node_input, operation_resolver, auth, timeout
            )
        else:
            error = f"unknown node type '{ntype}'"

        status = "failed" if error else "success"
        if status == "success":
            outputs[key] = output
            final_output = output

        # accumulate mapping warnings into the error field (non-fatal, §4).
        err_text = error
        if map_warnings:
            joined = "; ".join(map_warnings)
            err_text = f"{error} | warnings: {joined}" if error else f"warnings: {joined}"

        log = {
            "node_key": key,
            "seq": seq,
            "status": status,
            "input": node_input,
            "output": output,
            "error": err_text,
            "timestamp": _utcnow(),
        }
        logs.append(log)
        await _emit(on_node_event, log)
        seq += 1

        if status == "failed":
            aborted = True  # stop here; remaining nodes -> skipped

    # prepend any topo warnings as an informational first log entry
    if topo_warnings:
        logs.insert(
            0,
            {
                "node_key": "__graph__",
                "seq": -1,
                "status": "success",
                "input": None,
                "output": None,
                "error": "warnings: " + "; ".join(topo_warnings),
                "timestamp": started_at,
            },
        )

    overall_status = "failed" if aborted else "success"
    return {
        "execution_id": 0,                # placeholder; backend overwrites
        "workflow_id": workflow_id,
        "status": overall_status,
        "started_at": started_at,
        "finished_at": _utcnow(),
        "result": None if aborted else final_output,
        "logs": logs,
    }


__all__ = ["run_workflow", "resolve_path", "CycleError"]
