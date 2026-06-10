"""workflows + nodes + edges CRUD and graph (de)serialization (contract §2/§4/§5)."""
from __future__ import annotations

import sqlite3
from typing import Optional

from ..models import (
    DataMappingItem,
    Edge,
    Node,
    NodeParams,
    Position,
    WorkflowDetail,
    WorkflowGraph,
    WorkflowSummary,
)
from ._util import dumps, loads, utc_now


# --------------------------------------------------------------------------
# workflows table
# --------------------------------------------------------------------------


def _row_to_summary(r: sqlite3.Row) -> WorkflowSummary:
    return WorkflowSummary(
        id=r["id"],
        name=r["name"],
        description=r["description"],
        mcp_exposed=bool(r["mcp_exposed"]),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def create_workflow(
    conn: sqlite3.Connection, *, name: str, description: Optional[str]
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO workflows (name, description, mcp_exposed, created_at, updated_at)
        VALUES (?, ?, 0, ?, ?)
        """,
        (name, description, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_workflows(conn: sqlite3.Connection) -> list[WorkflowSummary]:
    rows = conn.execute("SELECT * FROM workflows ORDER BY id DESC").fetchall()
    return [_row_to_summary(r) for r in rows]


def get_workflow_row(conn: sqlite3.Connection, wf_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM workflows WHERE id = ?", (wf_id,)).fetchone()


def delete_workflow(conn: sqlite3.Connection, wf_id: int) -> bool:
    cur = conn.execute("DELETE FROM workflows WHERE id = ?", (wf_id,))
    conn.commit()
    return cur.rowcount > 0


def set_mcp_exposed(conn: sqlite3.Connection, wf_id: int, exposed: bool) -> bool:
    cur = conn.execute(
        "UPDATE workflows SET mcp_exposed = ?, updated_at = ? WHERE id = ?",
        (1 if exposed else 0, utc_now(), wf_id),
    )
    conn.commit()
    return cur.rowcount > 0


def _touch_workflow_meta(
    conn: sqlite3.Connection,
    wf_id: int,
    name: Optional[str],
    description: Optional[str],
) -> None:
    """Update updated_at, plus name/description if provided."""
    sets = ["updated_at = ?"]
    args: list = [utc_now()]
    if name is not None:
        sets.append("name = ?")
        args.append(name)
    if description is not None:
        sets.append("description = ?")
        args.append(description)
    args.append(wf_id)
    conn.execute(f"UPDATE workflows SET {', '.join(sets)} WHERE id = ?", args)


# --------------------------------------------------------------------------
# nodes / edges  <->  graph
# --------------------------------------------------------------------------


def _load_nodes(conn: sqlite3.Connection, wf_id: int) -> list[Node]:
    rows = conn.execute(
        "SELECT * FROM nodes WHERE workflow_id = ? ORDER BY id", (wf_id,)
    ).fetchall()
    nodes: list[Node] = []
    for r in rows:
        params_raw = loads(r["params"], {}) or {}
        nodes.append(
            Node(
                id=r["node_key"],
                type=r["type"],
                label=r["label"],
                operation_id=r["operation_id"],
                params=NodeParams(
                    path=params_raw.get("path", {}) or {},
                    query=params_raw.get("query", {}) or {},
                    header=params_raw.get("header", {}) or {},
                    body=params_raw.get("body"),
                ),
                position=Position(x=r["position_x"], y=r["position_y"]),
            )
        )
    return nodes


def _load_edges(conn: sqlite3.Connection, wf_id: int) -> list[Edge]:
    rows = conn.execute(
        "SELECT * FROM edges WHERE workflow_id = ? ORDER BY id", (wf_id,)
    ).fetchall()
    edges: list[Edge] = []
    for r in rows:
        mapping_raw = loads(r["data_mapping"], []) or []
        mapping = [
            DataMappingItem.model_validate(m)  # accepts wire key "from"
            for m in mapping_raw
        ]
        edges.append(
            Edge(
                id=r["edge_key"],
                source=r["source_node_key"],
                target=r["target_node_key"],
                data_mapping=mapping,
            )
        )
    return edges


def get_workflow_detail(
    conn: sqlite3.Connection, wf_id: int
) -> Optional[WorkflowDetail]:
    r = get_workflow_row(conn, wf_id)
    if r is None:
        return None
    return WorkflowDetail(
        id=r["id"],
        name=r["name"],
        description=r["description"],
        mcp_exposed=bool(r["mcp_exposed"]),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        nodes=_load_nodes(conn, wf_id),
        edges=_load_edges(conn, wf_id),
    )


def load_graph(conn: sqlite3.Connection, wf_id: int) -> Optional[WorkflowGraph]:
    """Load nodes+edges as a WorkflowGraph for the engine (§4)."""
    r = get_workflow_row(conn, wf_id)
    if r is None:
        return None
    return WorkflowGraph(
        workflow_id=wf_id,
        nodes=_load_nodes(conn, wf_id),
        edges=_load_edges(conn, wf_id),
    )


def replace_graph(
    conn: sqlite3.Connection,
    wf_id: int,
    nodes: list[Node],
    edges: list[Edge],
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> None:
    """Transactionally replace a workflow's nodes+edges (PUT semantics, §5).

    Deletes existing nodes/edges then inserts the new set. node_key/edge_key
    string identifiers are preserved as-is. Wraps everything in one
    transaction so a failure leaves the prior graph intact.
    """
    try:
        conn.execute("DELETE FROM edges WHERE workflow_id = ?", (wf_id,))
        conn.execute("DELETE FROM nodes WHERE workflow_id = ?", (wf_id,))

        for n in nodes:
            params_obj = {
                "path": n.params.path,
                "query": n.params.query,
                "header": n.params.header,
                "body": n.params.body,
            }
            conn.execute(
                """
                INSERT INTO nodes (workflow_id, node_key, operation_id, type,
                                   label, params, position_x, position_y)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    wf_id,
                    n.id,
                    n.operation_id,
                    n.type,
                    n.label,
                    dumps(params_obj),
                    n.position.x,
                    n.position.y,
                ),
            )

        for e in edges:
            mapping_wire = [
                {"from": m.from_, "to": m.to} for m in e.data_mapping
            ]
            conn.execute(
                """
                INSERT INTO edges (workflow_id, edge_key, source_node_key,
                                   target_node_key, data_mapping)
                VALUES (?, ?, ?, ?, ?)
                """,
                (wf_id, e.id, e.source, e.target, dumps(mapping_wire)),
            )

        _touch_workflow_meta(conn, wf_id, name, description)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
