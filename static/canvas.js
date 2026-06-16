/* =============================================================================
 * canvas.js — Drawflow <-> 계약 §4 그래프 직렬화 + 에디터 상호작용
 *
 * 단일 진실 공급원: _workspace/01_architect_contracts.md (§4 그래프 JSON, §5 REST).
 * 응답/요청 shape 은 추측하지 않고 계약 키를 그대로 사용한다.
 *
 * 계약 §4 shape (와이어 포맷, 키 고정):
 *   Node = { id:string, type:"api_call"|"start"|"end"|"transform", label:string,
 *            operation_id:int|null,
 *            params:{ path:{}, query:{}, header:{}, body:object|null },
 *            position:{ x:number, y:number } }
 *   Edge = { id:string, source:string, target:string,
 *            data_mapping:[ {from:string, to:string} ] }
 *   PUT body (WorkflowSaveRequest) = { nodes:Node[], edges:Edge[], name?, description? }
 *
 * Drawflow node.data 보관 형태:
 *   { ctype, label, operation_id, params, operation }   // ctype = 계약 node.type
 *   (Drawflow 의 'class' 라는 단어와 충돌 피하려고 ctype 사용. operation 은 팔레트
 *    OperationOut 원본 — 파라미터 폼 렌더용, 직렬화에는 안 들어간다.)
 * ========================================================================== */

const API = "/api";

// 노드/오퍼레이션에 base_url 미설정 시 서버가 사용하는 기본값.
// (서버 측 MCP_DEFAULT_BASE_URL 과 동일하게 유지 — 플레이스홀더 표시용)
const DEFAULT_BASE_URL = "http://localhost:8000";

let editor = null;          // Drawflow 인스턴스
let currentWorkflowId = null;
let selectedNodeId = null;  // Drawflow 숫자 id
let selectedEdge = null;    // {source, target} Drawflow ids
let nodeSeq = 0;            // node_key 발급용 카운터
let edgeSeq = 0;            // edge_key 발급용 카운터
// Drawflow 숫자 노드 id -> 계약 node_key("node_N") 매핑 보존
const edgeMappings = {};    // "df_src->df_tgt" -> [ {from,to} ]
const runFormCache = {};    // 실행 폼 마지막 입력값 보존(세션): {authToken, authType}
let runMode = "form";       // 실행 다이얼로그 모드: "form" | "json"
let runAuthType = "bearer"; // 인증 타입: "bearer" | "apikey"
let selectedNodes = new Set(); // 다중 선택된 Drawflow id(문자열) — 맞춤/분배용

// ---- operation 메타 캐시 (by OperationOut.id) --------------------------------
// 스펙 업로드 응답의 operations[] 로 즉시 채우고, 저장된 워크플로우 재진입 시에는
// GET /api/operations/{id} 로 lazy fetch 한다. 이 캐시 덕분에 로드 후에도 각 노드의
// 응답/입력 필드를 복원해 매핑 편집기가 클릭 삽입·auto-map 을 제공할 수 있다.
const operationCache = {};      // operation_id(int) -> OperationOut
const operationPending = {};    // operation_id(int) -> Promise (중복 fetch 방지)

function cacheOperations(ops) {
  (ops || []).forEach(op => { if (op && op.id != null) operationCache[op.id] = op; });
}

// operation 메타를 보장한다. 캐시에 있으면 즉시, 없으면 fetch 후 캐시. 실패 시 null.
async function ensureOperation(operationId) {
  if (operationId == null) return null;
  if (operationCache[operationId]) return operationCache[operationId];
  if (operationPending[operationId]) return operationPending[operationId];
  const p = (async () => {
    try {
      const res = await fetch(`${API}/operations/${operationId}`);
      if (!res.ok) return null;
      const op = await res.json();      // OperationOut
      if (op && op.id != null) operationCache[op.id] = op;
      return op;
    } catch { return null; }
    finally { delete operationPending[operationId]; }
  })();
  operationPending[operationId] = p;
  return p;
}

/* ------------------------------------------------------------------ init -- */
function initEditor(workflowId) {
  currentWorkflowId = workflowId;
  syncThemeIcon();
  const container = document.getElementById("drawflow");
  editor = new Drawflow(container);
  editor.reroute = true;
  editor.start();

  // 줌 배율 표시 갱신
  editor.on("zoom", updateZoomLabel);
  updateZoomLabel();

  // 다중 선택(맞춤/분배용) — 클릭 위임, shift로 누적
  container.addEventListener("click", onCanvasClickSelect);

  // 캔버스 드롭 핸들링 (팔레트에서 operation 드래그)
  container.addEventListener("dragover", (e) => e.preventDefault());
  container.addEventListener("drop", onCanvasDrop);

  // 노드 선택 -> 파라미터 패널
  editor.on("nodeSelected", (id) => {
    selectedNodeId = id;
    selectedEdge = null;
    renderNodeParams(id);
  });
  editor.on("nodeUnselected", () => {
    selectedNodeId = null;
    renderEmptyParams();
  });

  // 엣지 선택 -> data_mapping 편집
  editor.on("connectionSelected", (conn) => {
    // conn = {output_id, input_id, output_class, input_class}
    selectedEdge = { source: String(conn.output_id), target: String(conn.input_id) };
    selectedNodeId = null;
    switchTab("params");
    renderEdgeMapping(selectedEdge);
  });
  editor.on("connectionUnselected", () => {
    selectedEdge = null;
    renderEmptyParams();
  });

  // 엣지 생성/삭제 시 매핑 키 동기화
  editor.on("connectionCreated", (conn) => {
    const key = connKey(conn.output_id, conn.input_id);
    if (!edgeMappings[key]) edgeMappings[key] = [];
    // 새 엣지 연결 시 이름 매칭으로 data_mapping 미리 채움(있던 매핑은 건드리지 않음).
    if (!edgeMappings[key].length) {
      const e = { source: String(conn.output_id), target: String(conn.input_id) };
      resolveEdgeOps(e).then(({ srcOp, tgtOp }) => {
        // 그 사이 사용자가 직접 채웠으면 덮어쓰지 않음
        if (edgeMappings[key] && edgeMappings[key].length) return;
        const proposed = autoMapMappings(responseFieldPaths(srcOp), paramInputPaths(tgtOp));
        if (proposed.length) {
          edgeMappings[key] = proposed;
          // 현재 그 엣지를 보고 있으면 즉시 다시 그림
          if (selectedEdge && selectedEdge.source === e.source && selectedEdge.target === e.target) {
            renderEdgeMapping(selectedEdge);
          }
        }
      });
    }
  });
  editor.on("connectionRemoved", (conn) => {
    delete edgeMappings[connKey(conn.output_id, conn.input_id)];
  });

  loadWorkflow(workflowId);
}

function connKey(src, tgt) { return `${src}->${tgt}`; }

/* ------------------------------------------------------- node 생성 helpers - */
// Drawflow addNode -> 숫자 id 반환. node.data.node_key 에 계약 키 보존.
function makeNodeKey() { return `node_${nodeSeq++}`; }

function nodeHtml(label, sub) {
  // api 노드 라벨은 "METHOD /path" 형식 → 상단 상태바 + 메서드 배지 + 경로 + 제목(요약).
  const m = /^(GET|POST|PUT|PATCH|DELETE)\s+(.*)$/i.exec(label || "");
  if (m) {
    const method = m[1].toUpperCase();
    const title = sub || m[2];          // 요약 없으면 경로로 폴백
    return `<div class="wf-node" data-status="">
      <div class="wf-node__stripe"></div>
      <div class="wf-node__body">
        <div class="wf-node__head">
          <span class="wf-badge wf-badge--${method.toLowerCase()}">${escapeHtml(method)}</span>
          <span class="wf-path">${escapeHtml(m[2])}</span>
          <span class="wf-node__status"></span>
        </div>
        <div class="wf-node__title">${escapeHtml(title)}</div>
      </div>
    </div>`;
  }
  // start / end → 아이콘 타일 + 라벨
  if (label === "start" || label === "end") {
    const isStart = label === "start";
    const ico = isStart
      ? '<svg width="11" height="11" viewBox="0 0 10 10" aria-hidden="true"><path d="M2 1 L9 5 L2 9 Z" fill="#fff"/></svg>'
      : '<svg width="9" height="9" viewBox="0 0 10 10" aria-hidden="true"><rect x="1.5" y="1.5" width="7" height="7" rx="1.5" fill="#fff"/></svg>';
    return `<div class="wf-node wf-node--terminal wf-node--${isStart ? "start" : "end"}">
      <span class="wf-ico">${ico}</span>
      <span class="wf-label">${isStart ? "시작" : "종료"}</span>
    </div>`;
  }
  return `<div class="wf-node"><div class="wf-node__body">
    <div class="wf-node__title">${escapeHtml(label)}</div></div></div>`;
}

/* ----- 줌 컨트롤 ----- */
function updateZoomLabel() {
  const el = document.getElementById("zoom-label");
  if (el && editor) el.textContent = Math.round((editor.zoom || 1) * 100) + "%";
}
function zoomIn() { if (editor) { editor.zoom_in(); updateZoomLabel(); } }
function zoomOut() { if (editor) { editor.zoom_out(); updateZoomLabel(); } }
function zoomReset() { if (editor) { editor.zoom_reset(); updateZoomLabel(); } }

/* ----- 노드 위치 적용 (애니메이션) ----- */
function applyPos(id, x, y) {
  const home = editor.drawflow.drawflow.Home.data;
  if (!home[id]) return;
  const el = document.getElementById("node-" + id);
  if (el) {
    el.classList.add("lay-anim");
    el.style.left = x + "px";
    el.style.top = y + "px";
    setTimeout(() => el.classList.remove("lay-anim"), 260);
  }
  home[id].pos_x = x;
  home[id].pos_y = y;
  editor.updateConnectionNodes("node-" + id);
}

/* ----- 다중 선택 (맞춤/분배용) ----- */
function refreshMultiSel() {
  document.querySelectorAll(".drawflow .drawflow-node").forEach(n =>
    n.classList.toggle("multi-selected", selectedNodes.has(n.id.replace("node-", ""))));
  const n = selectedNodes.size;
  const countEl = document.getElementById("sel-count");
  if (countEl) {
    countEl.textContent = n >= 2 ? `${n}개 선택` : "";
    countEl.style.display = n >= 2 ? "" : "none";
  }
  document.querySelectorAll(".canvas-toolbar [data-needsel]").forEach(b => {
    b.disabled = n < 2;
  });
}

function onCanvasClickSelect(e) {
  const nodeEl = e.target.closest(".drawflow-node");
  if (!nodeEl) { selectedNodes.clear(); refreshMultiSel(); return; }
  const id = nodeEl.id.replace("node-", "");
  if (e.shiftKey) {
    selectedNodes.has(id) ? selectedNodes.delete(id) : selectedNodes.add(id);
  } else {
    selectedNodes = new Set([id]);
  }
  refreshMultiSel();
}

// 선택 노드들 크기/위치 수집.
function selectedRects() {
  const home = editor.drawflow.drawflow.Home.data;
  const rects = [];
  selectedNodes.forEach(id => {
    if (!home[id]) return;
    const el = document.getElementById("node-" + id);
    rects.push({
      id,
      x: home[id].pos_x, y: home[id].pos_y,
      w: el ? el.offsetWidth : 180, h: el ? el.offsetHeight : 60,
    });
  });
  return rects;
}

// 맞춤 정렬: top/bottom/left/right/centerx(수직 중앙선)/centery(수평 중앙선)
function alignNodes(mode) {
  const r = selectedRects();
  if (r.length < 2) return;
  const minX = Math.min(...r.map(n => n.x));
  const maxR = Math.max(...r.map(n => n.x + n.w));
  const minY = Math.min(...r.map(n => n.y));
  const maxB = Math.max(...r.map(n => n.y + n.h));
  const cx = (minX + maxR) / 2, cy = (minY + maxB) / 2;
  r.forEach(n => {
    let x = n.x, y = n.y;
    if (mode === "left") x = minX;
    else if (mode === "right") x = maxR - n.w;
    else if (mode === "centerx") x = cx - n.w / 2;
    else if (mode === "top") y = minY;
    else if (mode === "bottom") y = maxB - n.h;
    else if (mode === "centery") y = cy - n.h / 2;
    applyPos(n.id, Math.round(x), Math.round(y));
  });
  setSaveStatus("맞춤 정렬됨 (저장 필요)", false);
}

// 균등 분배: 축(x|y)으로 사이 간격을 균등하게.
function distributeNodes(axis) {
  const r = selectedRects();
  if (r.length < 3) { setSaveStatus("균등 분배는 3개 이상 선택", true); return; }
  const key = axis === "y" ? "y" : "x";
  const sizeKey = axis === "y" ? "h" : "w";
  r.sort((a, b) => a[key] - b[key]);
  const first = r[0], last = r[r.length - 1];
  const span = (last[key] + last[sizeKey]) - first[key];
  const totalW = r.reduce((s, n) => s + n[sizeKey], 0);
  const gap = (span - totalW) / (r.length - 1);
  let cursor = first[key];
  r.forEach(n => {
    const pos = Math.round(cursor);
    if (axis === "y") applyPos(n.id, n.x, pos); else applyPos(n.id, pos, n.y);
    cursor += n[sizeKey] + gap;
  });
  setSaveStatus("균등 분배됨 (저장 필요)", false);
}

/* ----- 자동 정렬 (좌→우 레이어드) ----- */
function autoLayout() {
  if (!editor) return;
  const home = editor.drawflow.drawflow.Home.data;   // live data
  const ids = Object.keys(home);
  if (!ids.length) return;

  const adj = {}, indeg = {};
  ids.forEach(id => { adj[id] = []; indeg[id] = 0; });
  ids.forEach(id => {
    const outs = home[id].outputs || {};
    for (const o in outs) for (const c of (outs[o].connections || [])) {
      const t = String(c.node);
      if (adj[id] && indeg[t] != null) { adj[id].push(t); indeg[t]++; }
    }
  });

  // longest-path layering (Kahn). roots(indeg 0) = layer 0.
  const layer = {}, deg = {};
  ids.forEach(id => { deg[id] = indeg[id]; });
  const queue = ids.filter(id => indeg[id] === 0);
  queue.forEach(id => { layer[id] = 0; });
  let qi = 0;
  while (qi < queue.length) {
    const u = queue[qi++];
    for (const v of adj[u]) {
      layer[v] = Math.max(layer[v] || 0, (layer[u] || 0) + 1);
      if (--deg[v] === 0) queue.push(v);
    }
  }
  ids.forEach(id => { if (layer[id] == null) layer[id] = 0; }); // cycle fallback

  const byLayer = {};
  ids.forEach(id => { (byLayer[layer[id]] = byLayer[layer[id]] || []).push(id); });

  const COLX = 300, ROWY = 150, X0 = 60, Y0 = 60;
  Object.keys(byLayer).sort((a, b) => a - b).forEach(L => {
    byLayer[L].forEach((id, i) => {
      applyPos(id, X0 + (+L) * COLX, Y0 + i * ROWY);
    });
  });
  setSaveStatus("자동 정렬됨 (저장 필요)", false);
}

/* ----- 테마 토글 ----- */
function syncThemeIcon() {
  const b = document.getElementById("theme-toggle");
  if (b) b.textContent =
    document.documentElement.getAttribute("data-theme") === "dark" ? "🌙" : "🌗";
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("mcp-theme", next); } catch (e) { /* noop */ }
  syncThemeIcon();
}

// 실행 결과의 노드 상태를 캔버스 노드 색으로 반영.
function applyNodeStatuses(logs) {
  // 초기화
  document.querySelectorAll(".drawflow .wf-node").forEach(wf => {
    wf.setAttribute("data-status", "");
    const s = wf.querySelector(".wf-node__status");
    if (s) { s.textContent = ""; s.className = "wf-node__status"; }
  });
  let home;
  try { home = editor.drawflow.drawflow.Home.data; } catch (e) { return; }
  const keyToDf = {};
  for (const id in home) {
    const k = home[id].data && home[id].data.node_key;
    if (k) keyToDf[k] = id;
  }
  const statusAttr = { success: "success", failed: "error", skipped: "skipped" };
  const mark = { success: "✓", failed: "!", skipped: "스킵" };
  for (const l of (logs || [])) {
    const dfId = keyToDf[l.node_key];
    if (!dfId) continue;
    const el = document.getElementById("node-" + dfId);
    if (!el) continue;
    const wf = el.querySelector(".wf-node");
    if (!wf) continue;
    wf.setAttribute("data-status", statusAttr[l.status] || "");
    const s = wf.querySelector(".wf-node__status");
    if (s) {
      s.textContent = mark[l.status] || "";
      if (l.status === "success") s.classList.add("wf-node__status--success");
      else if (l.status === "failed") s.classList.add("wf-node__status--error");
    }
  }
}

function addApiNode(operation, posX, posY) {
  if (operation && operation.id != null) operationCache[operation.id] = operation;
  const label = `${operation.method} ${operation.path}`;
  const data = {
    node_key: makeNodeKey(),
    ctype: "api_call",
    label: label,
    operation_id: operation.id,            // OperationOut.id (= operations.id DB PK)
    base_url: null,                        // 노드별 base_url 오버라이드(빈 값=오퍼레이션/기본값 사용)
    params: { path: {}, query: {}, header: {}, body: null },
    operation: operation,                  // 폼 렌더용 원본(직렬화 제외)
  };
  // inputs=1, outputs=1 : 순차 실행 그래프
  editor.addNode(
    "api_call", 1, 1, posX, posY, "api_call",
    data, nodeHtml(label, operation.summary || "")
  );
}

function addStartNode() {
  const data = {
    node_key: makeNodeKey(), ctype: "start", label: "start",
    operation_id: null, params: { path: {}, query: {}, header: {}, body: null },
    operation: null,
  };
  editor.addNode("start", 0, 1, 60, 80, "start", data, nodeHtml("start", "외부 입력"));
}

function addEndNode() {
  const data = {
    node_key: makeNodeKey(), ctype: "end", label: "end",
    operation_id: null, params: { path: {}, query: {}, header: {}, body: null },
    operation: null,
  };
  editor.addNode("end", 1, 0, 700, 80, "end", data, nodeHtml("end", "최종 결과"));
}

function onCanvasDrop(e) {
  e.preventDefault();
  const raw = e.dataTransfer.getData("application/json");
  if (!raw) return;
  let op;
  try { op = JSON.parse(raw); } catch { return; }
  const rect = e.currentTarget.getBoundingClientRect();
  const zoom = editor.zoom || 1;
  const x = (e.clientX - rect.left) / zoom - (editor.canvas_x / zoom);
  const y = (e.clientY - rect.top) / zoom - (editor.canvas_y / zoom);
  addApiNode(op, x, y);
}

/* ================================================================ §4 직렬화 = */
/**
 * Drawflow export -> 계약 §4 그래프.
 * 반환: { nodes:Node[], edges:Edge[] }  (PUT WorkflowSaveRequest 의 nodes/edges)
 */
function toContractGraph(drawflowExport) {
  const home = drawflowExport.drawflow.Home.data; // {dfId: {id, data, pos_x, pos_y, outputs,...}}
  const nodes = [];
  const edges = [];
  // Drawflow 숫자 id -> 계약 node_key
  const idToKey = {};

  for (const dfId in home) {
    const n = home[dfId];
    const d = n.data || {};
    const key = d.node_key || `node_${dfId}`;
    idToKey[String(n.id)] = key;
  }

  for (const dfId in home) {
    const n = home[dfId];
    const d = n.data || {};
    const p = d.params || {};
    nodes.push({
      id: idToKey[String(n.id)],
      type: d.ctype || "api_call",
      label: d.label || "",
      operation_id: (d.operation_id === undefined ? null : d.operation_id),
      base_url: (d.base_url ? d.base_url : null),
      params: {
        path:   p.path   || {},
        query:  p.query  || {},
        header: p.header || {},
        body:   (p.body === undefined ? null : p.body),
      },
      position: { x: n.pos_x, y: n.pos_y },
    });

    // 출력 연결 -> edges
    const outputs = n.outputs || {};
    for (const outName in outputs) {
      const conns = outputs[outName].connections || [];
      for (const c of conns) {
        const srcKey = idToKey[String(n.id)];
        const tgtKey = idToKey[String(c.node)];
        const mapping = edgeMappings[connKey(n.id, c.node)] || [];
        edges.push({
          id: `edge_${edgeSeq++}`,
          source: srcKey,
          target: tgtKey,
          // 와이어 키 문자열 "from","to" 고정
          data_mapping: mapping.map(m => ({ from: m.from, to: m.to })),
        });
      }
    }
  }
  return { nodes, edges };
}

/**
 * 계약 §4 그래프 -> Drawflow import.
 * graph: { nodes:Node[], edges:Edge[] } (WorkflowDetail 에서 발췌)
 */
function fromContractGraph(graph) {
  editor.clear();
  const exportObj = { drawflow: { Home: { data: {} } } };
  const home = exportObj.drawflow.Home.data;
  const keyToDfId = {};
  let dfCounter = 1;
  let maxNodeSeq = 0;

  const nodes = graph.nodes || [];
  const edges = graph.edges || [];

  // 1) 노드 배치
  for (const node of nodes) {
    const dfId = dfCounter++;
    keyToDfId[node.id] = dfId;
    const m = /node_(\d+)/.exec(node.id);
    if (m) maxNodeSeq = Math.max(maxNodeSeq, parseInt(m[1], 10) + 1);

    const ctype = node.type || "api_call";
    const inputsCount = ctype === "start" ? 0 : 1;
    const outputsCount = ctype === "end" ? 0 : 1;
    const inputs = {};
    const outputs = {};
    for (let i = 1; i <= inputsCount; i++) inputs[`input_${i}`] = { connections: [] };
    for (let i = 1; i <= outputsCount; i++) outputs[`output_${i}`] = { connections: [] };

    const data = {
      node_key: node.id,
      ctype: ctype,
      label: node.label || "",
      operation_id: (node.operation_id === undefined ? null : node.operation_id),
      base_url: (node.base_url ? node.base_url : null),
      params: {
        path:   (node.params && node.params.path)   || {},
        query:  (node.params && node.params.query)  || {},
        header: (node.params && node.params.header) || {},
        body:   (node.params ? node.params.body : null),
      },
      operation: null, // 로드 시 OperationOut 원본은 없음(필요하면 operation_id 로 재조회)
    };

    home[dfId] = {
      id: dfId,
      name: ctype,
      data: data,
      class: ctype,
      html: nodeHtml(data.label || ctype, ""),
      typenode: false,
      inputs: inputs,
      outputs: outputs,
      pos_x: (node.position && node.position.x) || 0,
      pos_y: (node.position && node.position.y) || 0,
    };
  }
  nodeSeq = Math.max(nodeSeq, maxNodeSeq);

  // 2) 엣지 연결 + 매핑 복원
  let maxEdgeSeq = 0;
  for (const edge of edges) {
    const srcDf = keyToDfId[edge.source];
    const tgtDf = keyToDfId[edge.target];
    if (srcDf == null || tgtDf == null) continue;
    const srcNode = home[srcDf];
    const tgtNode = home[tgtDf];
    const outName = Object.keys(srcNode.outputs)[0] || "output_1";
    const inName = Object.keys(tgtNode.inputs)[0] || "input_1";
    srcNode.outputs[outName].connections.push({ node: String(tgtDf), output: inName });
    tgtNode.inputs[inName].connections.push({ node: String(srcDf), input: outName });
    // data_mapping 보존 (와이어 키 from/to)
    edgeMappings[connKey(srcDf, tgtDf)] =
      (edge.data_mapping || []).map(m => ({ from: m.from, to: m.to }));
    const em = /edge_(\d+)/.exec(edge.id || "");
    if (em) maxEdgeSeq = Math.max(maxEdgeSeq, parseInt(em[1], 10) + 1);
  }
  edgeSeq = Math.max(edgeSeq, maxEdgeSeq);

  editor.import(exportObj);
}

/* ============================================================= REST 연동 ===== */
async function loadWorkflow(id) {
  try {
    const res = await fetch(`${API}/workflows/${id}`);
    if (!res.ok) throw new Error(`워크플로우 로드 실패 (${res.status})`);
    const wf = await res.json(); // WorkflowDetail {id,name,description,mcp_exposed,...,nodes,edges}
    document.getElementById("wf-title").textContent = wf.name || `워크플로우 #${id}`;
    setExposeBtn(!!wf.mcp_exposed);
    const gi = document.getElementById("mcp-group");
    if (gi) gi.value = wf.mcp_group || "";
    const ti = document.getElementById("mcp-tool-name");
    if (ti) ti.value = wf.mcp_tool_name || "";
    fromContractGraph({ nodes: wf.nodes || [], edges: wf.edges || [] });
    refreshNodeTitles();   // 오퍼레이션 요약을 노드 제목에 반영(경로 중복 방지)
  } catch (e) {
    setSaveStatus(e.message, true);
  }
}

// 로드된 api 노드의 제목을 오퍼레이션 요약으로 갱신(요약 없으면 경로 유지).
async function refreshNodeTitles() {
  const home = editor.drawflow.drawflow.Home.data;
  for (const id in home) {
    const d = home[id].data || {};
    if (d.ctype !== "api_call" || d.operation_id == null) continue;
    const op = await ensureOperation(d.operation_id);
    if (!op) continue;
    d.operation = op;  // 속성 패널용 캐시
    if (!op.summary) continue;
    const el = document.getElementById("node-" + id);
    const t = el && el.querySelector(".wf-node__title");
    if (t) t.textContent = op.summary;
  }
}

let currentExposed = false;
function setExposeBtn(exposed) {
  currentExposed = !!exposed;
  const btn = document.getElementById("expose-btn");
  if (!btn) return;
  btn.textContent = "노출: " + (currentExposed ? "ON" : "OFF");
  btn.classList.toggle("primary", currentExposed);
}

// 도구 이름 입력 변경 시 노출 상태 유지한 채 이름만 저장.
async function applyToolName() {
  const el = document.getElementById("mcp-tool-name");
  const toolName = el ? (el.value || "").trim() : "";
  try {
    const res = await fetch(`${API}/workflows/${currentWorkflowId}/expose`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exposed: currentExposed, tool_name: toolName }),
    });
    if (!res.ok) throw new Error(`도구 이름 저장 실패 (${res.status})`);
    const r = await res.json();
    if (el) el.value = r.mcp_tool_name || "";
    setSaveStatus(`도구 이름=${r.mcp_tool_name || "(자동)"} 저장됨 (Claude Desktop 재시작 필요)`, false);
  } catch (e) {
    setSaveStatus(e.message, true);
  }
}

// 그룹 입력 변경 시 노출 상태는 유지한 채 그룹만 저장.
async function applyMcpGroup() {
  const groupEl = document.getElementById("mcp-group");
  const group = groupEl ? (groupEl.value || "").trim() : "";
  try {
    const res = await fetch(`${API}/workflows/${currentWorkflowId}/expose`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exposed: currentExposed, group: group }),
    });
    if (!res.ok) throw new Error(`그룹 저장 실패 (${res.status})`);
    const r = await res.json();
    if (groupEl) groupEl.value = r.mcp_group || "";
    setSaveStatus(`그룹=${r.mcp_group || "(기본)"} 저장됨 (Claude Desktop 재시작 필요)`, false);
  } catch (e) {
    setSaveStatus(e.message, true);
  }
}

// MCP 노출 토글. 변경 후에는 MCP 클라이언트(예: Claude Desktop) 재시작 필요.
async function toggleExpose() {
  const next = !currentExposed;
  const groupEl = document.getElementById("mcp-group");
  const group = groupEl ? (groupEl.value || "").trim() : "";
  try {
    const res = await fetch(`${API}/workflows/${currentWorkflowId}/expose`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exposed: next, group: group }),
    });
    if (!res.ok) throw new Error(`노출 변경 실패 (${res.status})`);
    const r = await res.json();
    setExposeBtn(!!r.mcp_exposed);
    if (groupEl) groupEl.value = r.mcp_group || "";
    setSaveStatus(next
      ? `MCP 노출됨 ✓ 그룹=${r.mcp_group || "(기본)"} (Claude Desktop 재시작 필요)`
      : "MCP 노출 해제됨", false);
  } catch (e) {
    setSaveStatus(e.message, true);
  }
}

async function saveWorkflow() {
  try {
    const graph = toContractGraph(editor.export());
    const body = { nodes: graph.nodes, edges: graph.edges }; // WorkflowSaveRequest
    const res = await fetch(`${API}/workflows/${currentWorkflowId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`저장 실패 (${res.status})`);
    await res.json(); // WorkflowDetail
    setSaveStatus("저장됨 ✓", false);
  } catch (e) {
    setSaveStatus(e.message, true);
  }
}

function setSaveStatus(msg, isErr) {
  const el = document.getElementById("save-status");
  el.textContent = msg;
  el.className = isErr ? "error-msg" : "muted";
  if (!isErr) setTimeout(() => { el.textContent = ""; }, 2500);
}

/* ----- 실행 ----- */
async function openRunDialog() {
  document.getElementById("run-error").textContent = "";
  setRunModeUI("form");
  setAuthType(runFormCache.authType || "bearer");
  document.getElementById("run-auth-token").value = runFormCache.authToken || "";
  await buildRunForm();
  updateRunSubtitle();
  document.getElementById("run-dialog").showModal();
}
function closeRunDialog() {
  document.getElementById("run-dialog").close();
}

// 다이얼로그 서브타이틀: 연결된 API 노드 수.
function updateRunSubtitle() {
  const el = document.getElementById("run-sub");
  if (!el) return;
  const n = startConnectedApiNodeIds().length;
  el.textContent = n ? `API 노드 ${n}개` : "연결된 API 노드 없음";
}

// 인증 타입 세그먼트 선택.
function setAuthType(type) {
  runAuthType = (type === "apikey") ? "apikey" : "bearer";
  document.querySelectorAll("#run-auth-seg .seg-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.auth === runAuthType));
}

// 모드 UI만 전환(값 동기화 없음).
function setRunModeUI(mode) {
  runMode = (mode === "json") ? "json" : "form";
  document.getElementById("run-form-mode").hidden = runMode !== "form";
  document.getElementById("run-json-mode").hidden = runMode !== "json";
  document.querySelectorAll("#run-mode-seg .seg-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.mode === runMode));
}

// 시작 노드에 직접 연결된 api_call 노드들의 Drawflow id 목록.
function startConnectedApiNodeIds() {
  const home = editor.export().drawflow.Home.data;
  let startDf = null;
  for (const id in home) {
    if (home[id].data && home[id].data.ctype === "start") { startDf = id; break; }
  }
  if (startDf == null) return [];
  const targets = [];
  const outs = home[startDf].outputs || {};
  for (const o in outs) {
    for (const c of (outs[o].connections || [])) {
      const t = String(c.node);
      if (home[t] && home[t].data && home[t].data.ctype === "api_call") targets.push(t);
    }
  }
  return targets;
}

// to 경로 형식: params.<path|query|header>.<name> | params.body | params.body.<key>
function getNodeParamByPath(d, toPath) {
  const parts = String(toPath).split(".");
  const p = d.params || {};
  if (parts.length === 3 && ["path", "query", "header"].includes(parts[1]))
    return (p[parts[1]] || {})[parts[2]];
  if (parts.length === 2 && parts[1] === "body") return p.body;
  if (parts.length >= 3 && parts[1] === "body") {
    const b = p.body;
    return (b && typeof b === "object") ? b[parts.slice(2).join(".")] : undefined;
  }
  return undefined;
}

function setNodeParamByPath(d, toPath, value) {
  if (!d.params) d.params = { path: {}, query: {}, header: {}, body: null };
  const parts = String(toPath).split(".");
  if (parts.length === 3 && ["path", "query", "header"].includes(parts[1])) {
    const loc = parts[1], name = parts[2];
    if (!d.params[loc] || typeof d.params[loc] !== "object") d.params[loc] = {};
    if (value === undefined) delete d.params[loc][name]; else d.params[loc][name] = value;
    return;
  }
  if (parts.length === 2 && parts[1] === "body") {
    d.params.body = (value === undefined ? null : value); return;
  }
  if (parts.length >= 3 && parts[1] === "body") {
    const key = parts.slice(2).join(".");
    if (!d.params.body || typeof d.params.body !== "object") d.params.body = {};
    if (value === undefined) delete d.params.body[key]; else d.params.body[key] = value;
  }
}

function paramDisplay(v) {
  if (v == null) return "";
  return (typeof v === "string") ? v : JSON.stringify(v);
}

// 폼 렌더: 시작 노드에 직접 연결된 API 노드들의 파라미터를 노드별로 표시(현재값 prefill).
async function buildRunForm() {
  const wrap = document.getElementById("run-form-fields");
  const dfIds = startConnectedApiNodeIds();
  if (!dfIds.length) {
    wrap.innerHTML = '<p class="muted">시작 노드에 직접 연결된 API 노드가 없습니다. ' +
      'API 노드를 시작 노드에 연결하거나 "JSON 직접 편집"으로 입력하세요.</p>';
    return;
  }
  let html = "";
  for (const dfId of dfIds) {
    const node = editor.getNodeFromId(dfId);
    const d = (node && node.data) || {};
    const op = await ensureOperation(d.operation_id);
    const fields = paramInputPaths(op);  // [{path,label,loc,type,required}]
    const title = escapeHtml(d.label || ("node " + dfId));
    if (!fields.length) {
      html += `<fieldset><legend>${title}</legend>
        <p class="muted">입력 파라미터가 없습니다.</p></fieldset>`;
      continue;
    }
    html += `<fieldset><legend>${title}</legend>`;
    for (const f of fields) {
      const cur = paramDisplay(getNodeParamByPath(d, f.path));
      html += `<label class="pfield">
        <span>${escapeHtml(f.label)}${f.required ? ' <em>*</em>' : ''}
          <small class="muted">${escapeHtml(f.loc)}${f.type ? " · " + escapeHtml(f.type) : ""}</small></span>
        <input type="text" data-node-df="${escapeHtml(String(dfId))}" data-to-path="${escapeHtml(f.path)}"
          value="${escapeHtml(cur)}" placeholder="값 입력 (숫자/JSON 가능)" autocomplete="off" /></label>`;
    }
    html += `</fieldset>`;
  }
  wrap.innerHTML = html;
}

// 문자열 값을 JSON 으로 해석 시도(숫자/불리언/객체), 실패 시 원문 문자열.
function coerceVal(raw) {
  const t = String(raw).trim();
  if (t === "") return undefined;
  try { return JSON.parse(t); } catch { return raw; }
}

// 폼 입력값을 각 노드의 정적 params 에 기록(저장 시 영속, 속성 패널과 동일 데이터).
function applyRunFormToNodes() {
  const byDf = {};
  document.querySelectorAll('#run-form-fields [data-to-path]').forEach(el => {
    const dfId = el.dataset.nodeDf;
    (byDf[dfId] = byDf[dfId] || []).push({ to: el.dataset.toPath, value: coerceVal(el.value) });
  });
  for (const dfId in byDf) {
    const node = editor.getNodeFromId(dfId);
    if (!node) continue;
    const d = node.data;
    for (const item of byDf[dfId]) setNodeParamByPath(d, item.to, item.value);
    editor.updateNodeDataFromId(dfId, d);
  }
}

// 실행 입력 수집: 폼 모드는 노드 정적 params 에 반영하고 auth 만 수집(initial_input 은 빈 객체).
function collectRunForm() {
  applyRunFormToNodes();
  const token = document.getElementById("run-auth-token").value.trim();
  const auth = {};
  if (token) { if (runAuthType === "apikey") auth.api_key = token; else auth.token = token; }
  runFormCache.authToken = token;
  runFormCache.authType = runAuthType;
  return { initialInput: {}, auth };
}

// 폼 ↔ JSON 모드 전환(값 동기화).
async function setRunMode(mode) {
  if (mode === "json" && runMode !== "json") {
    // 폼값을 노드에 반영하고 JSON 텍스트 채우기
    const { initialInput, auth } = collectRunForm();
    document.getElementById("run-initial-input").value = JSON.stringify(initialInput, null, 2);
    document.getElementById("run-auth").value = JSON.stringify(auth, null, 2);
    setRunModeUI("json");
  } else if (mode === "form" && runMode !== "form") {
    // JSON → 폼: auth 복원 후 폼 재구성
    try {
      const a = JSON.parse(document.getElementById("run-auth").value || "{}");
      if (a.api_key != null) { runFormCache.authToken = String(a.api_key); setAuthType("apikey"); }
      else if (a.token != null) { runFormCache.authToken = String(a.token); setAuthType("bearer"); }
    } catch { /* noop */ }
    document.getElementById("run-auth-token").value = runFormCache.authToken || "";
    await buildRunForm();
    setRunModeUI("form");
  }
}

async function runWorkflow(ev) {
  ev.preventDefault();
  const errEl = document.getElementById("run-error");
  errEl.textContent = "";
  let initialInput, auth;
  if (runMode === "json") {
    try {
      initialInput = JSON.parse(document.getElementById("run-initial-input").value || "{}");
      auth = JSON.parse(document.getElementById("run-auth").value || "{}");
    } catch (e) {
      errEl.textContent = "JSON 형식 오류: " + e.message;
      return false;
    }
  } else {
    ({ initialInput, auth } = collectRunForm());
  }
  // 저장 후 실행(최신 그래프 보장)
  await saveWorkflow();
  try {
    // RunRequest = {initial_input, auth}
    const res = await fetch(`${API}/workflows/${currentWorkflowId}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ initial_input: initialInput, auth: auth }),
    });
    // 노드 실패도 HTTP 200 + status:"failed" (§11-3). HTTP 오류는 진짜 서버 오류만.
    if (!res.ok) throw new Error(`실행 요청 실패 (${res.status})`);
    const result = await res.json(); // ExecutionResult
    closeRunDialog();
    switchTab("log");
    renderExecution(result);
  } catch (e) {
    errEl.textContent = e.message;
  }
  return false;
}

/* ============================================================ 패널 렌더링 ==== */
function switchTab(name) {
  document.querySelectorAll(".tab").forEach(t =>
    t.classList.toggle("active", t.dataset.tab === name));
  document.getElementById("tab-params").hidden = (name !== "params");
  document.getElementById("tab-log").hidden = (name !== "log");
}

function renderEmptyParams() {
  document.getElementById("node-params").innerHTML =
    '<div class="params-empty muted">노드를 클릭하면 정적 파라미터를, 엣지를 클릭하면 데이터 매핑을 편집합니다.</div>';
}

// 노드 정적 params 폼 (path/query/header/body)
function renderNodeParams(dfId) {
  switchTab("params");
  const node = editor.getNodeFromId(dfId);
  const d = node.data || {};
  const op = d.operation;
  const params = d.params || { path: {}, query: {}, header: {}, body: null };
  const wrap = document.getElementById("node-params");

  if (d.ctype !== "api_call") {
    wrap.innerHTML = `<div class="params-head"><strong>${escapeHtml(d.label)}</strong>
      <span class="badge">${escapeHtml(d.ctype)}</span></div>
      <p class="muted">${d.ctype === "start"
        ? "외부 입력(initial_input)이 이 노드의 출력으로 노출됩니다."
        : "위상상 마지막 노드의 출력이 최종 결과가 됩니다."}</p>`;
    return;
  }

  // 로드된 노드는 d.operation 이 null 일 수 있다. 캐시/fetch 로 메타를 복원해
  // 동적 파라미터 폼을 재구성한다(과거 한계 개선). 캐시에 있으면 즉시 사용.
  if (!op && d.operation_id != null) {
    if (operationCache[d.operation_id]) {
      d.operation = operationCache[d.operation_id];
      editor.updateNodeDataFromId(dfId, d);
    } else {
      ensureOperation(d.operation_id).then(fetched => {
        if (fetched && selectedNodeId === dfId) {
          const nd = editor.getNodeFromId(dfId).data;
          nd.operation = fetched;
          editor.updateNodeDataFromId(dfId, nd);
          renderNodeParams(dfId);   // 메타 도착 후 폼 재렌더
        }
      });
    }
  }
  const opMeta = d.operation || op;
  const schema = (opMeta && opMeta.params_schema) || { path: [], query: [], header: [] };
  let html = `<div class="params-head"><strong>${escapeHtml(d.label)}</strong>
    <span class="badge">api_call</span></div>`;
  html += `<p class="hint">정적 기본값만 입력하세요. 동적 값은 엣지의 data_mapping 으로 주입됩니다(§7).</p>`;

  // Base URL (노드별 오버라이드). 빈 값이면 오퍼레이션/기본값 사용.
  const opBaseUrl = (opMeta && opMeta.base_url) ? opMeta.base_url : "";
  const baseUrlPlaceholder = opBaseUrl || DEFAULT_BASE_URL;
  const baseUrlVal = d.base_url || "";
  html += `<fieldset><legend>Base URL</legend>
    <label class="pfield">
      <span>호출 호스트 <small class="muted">노드별 오버라이드</small></span>
      <input type="text" data-loc="base_url"
        value="${escapeHtml(baseUrlVal)}"
        placeholder="${escapeHtml(baseUrlPlaceholder)}" />
    </label>
    <p class="hint">비워두면 ${opBaseUrl
      ? "오퍼레이션 기본값(<code>" + escapeHtml(opBaseUrl) + "</code>)"
      : "서버 기본값(<code>" + escapeHtml(DEFAULT_BASE_URL) + "</code>)"} 을 사용합니다.</p>
  </fieldset>`;

  for (const loc of ["path", "query", "header"]) {
    const list = schema[loc] || [];
    if (!list.length) continue;
    html += `<fieldset><legend>${loc}</legend>`;
    for (const f of list) {
      const val = params[loc][f.name] ?? "";
      html += `<label class="pfield">
        <span>${escapeHtml(f.name)}${f.required ? ' <em>*</em>' : ''}
          <small class="muted">${escapeHtml(f.type || "")}</small></span>`;
      if (Array.isArray(f.enum)) {
        html += `<select data-loc="${loc}" data-name="${escapeHtml(f.name)}">
          <option value="">(미설정)</option>` +
          f.enum.map(o => `<option ${String(val) === String(o) ? "selected" : ""}>${escapeHtml(o)}</option>`).join("") +
          `</select>`;
      } else {
        html += `<input type="text" data-loc="${loc}" data-name="${escapeHtml(f.name)}"
          value="${escapeHtml(val)}" placeholder="${escapeHtml(f.description || "")}" />`;
      }
      html += `</label>`;
    }
    html += `</fieldset>`;
  }

  // body (request_schema 존재 시)
  if (opMeta && opMeta.request_schema) {
    const bodyStr = params.body == null ? "" : JSON.stringify(params.body, null, 2);
    html += `<fieldset><legend>body (JSON)</legend>
      <textarea data-loc="body" rows="6" placeholder="{ ... }">${escapeHtml(bodyStr)}</textarea>
      <div class="warn-msg" id="body-err"></div></fieldset>`;
  }

  wrap.innerHTML = html;

  // 변경 -> 노드 data.params 즉시 반영
  wrap.querySelectorAll('[data-loc]').forEach(el => {
    el.addEventListener("change", () => applyParamChange(dfId, el));
  });
}

function applyParamChange(dfId, el) {
  const node = editor.getNodeFromId(dfId);
  const d = node.data;
  const loc = el.dataset.loc;
  if (loc === "base_url") {
    const v = el.value.trim();
    d.base_url = v || null;
    editor.updateNodeDataFromId(dfId, d);
    return;
  }
  if (loc === "body") {
    const errEl = document.getElementById("body-err");
    const txt = el.value.trim();
    if (!txt) { d.params.body = null; if (errEl) errEl.textContent = ""; }
    else {
      try { d.params.body = JSON.parse(txt); if (errEl) errEl.textContent = ""; }
      catch (e) { if (errEl) errEl.textContent = "JSON 오류: " + e.message; return; }
    }
  } else {
    const name = el.dataset.name;
    const v = el.value;
    if (v === "") delete d.params[loc][name];
    else d.params[loc][name] = v;
  }
  editor.updateNodeDataFromId(dfId, d);
}

/* ----- 스키마 → 필드 경로 추출 (계약 §3/§4) ----- */

// response_schema = {"200": <JSON Schema>, ...}. 200(없으면 첫 2xx/default)의 스키마에서
// top-level 필드를 JSONPath 부분집합 경로로 추출. type:object → $.<key>,
// type:array+items:object → $[0].<key>. 한 단계 중첩까지. 추출 불가 시 [].
function pickResponseSchema(responseSchema) {
  if (!responseSchema || typeof responseSchema !== "object") return null;
  if (responseSchema["200"]) return responseSchema["200"];
  const twoXX = Object.keys(responseSchema).find(k => /^2\d\d$/.test(k));
  if (twoXX) return responseSchema[twoXX];
  if (responseSchema["default"]) return responseSchema["default"];
  return null;
}

// 단일 객체 스키마의 properties → 경로 목록(prefix 기준). label/path 객체 배열 반환.
function objectFieldPaths(schema, prefix) {
  const out = [];
  if (!schema || typeof schema !== "object") return out;
  const props = schema.properties;
  if (!props || typeof props !== "object") return out;
  for (const key of Object.keys(props)) {
    const sub = props[key] || {};
    const path = `${prefix}.${key}`;
    const t = sub.type || (sub.properties ? "object" : "");
    out.push({ path, label: key, type: t });
  }
  return out;
}

// response_schema → from 후보 경로 목록([{path,label,type}]). 추출 불가 시 [].
function responseFieldPaths(op) {
  const schema = pickResponseSchema(op && op.response_schema);
  if (!schema) return [];
  if (schema.type === "array" && schema.items && typeof schema.items === "object") {
    if (schema.items.type === "object" || schema.items.properties) {
      return objectFieldPaths(schema.items, "$[0]");
    }
    return [{ path: "$[0]", label: "[0]", type: schema.items.type || "" }];
  }
  // type:object 또는 properties 존재
  if (schema.type === "object" || schema.properties) {
    return objectFieldPaths(schema, "$");
  }
  return [];
}

// params_schema(path/query/header) + requestBody → to 후보 경로 목록.
//   path[]/query[]/header[].name → params.<loc>.<name>
//   requestBody(request_schema.schema.properties) → params.body.<key> (없으면 params.body)
function paramInputPaths(op) {
  const out = [];
  if (!op) return out;
  const ps = op.params_schema || {};
  for (const loc of ["path", "query", "header"]) {
    const list = ps[loc] || [];
    for (const f of list) {
      if (!f || !f.name) continue;
      out.push({ path: `params.${loc}.${f.name}`, label: f.name, loc, type: f.type || "", required: !!f.required });
    }
  }
  const rs = op.request_schema;
  if (rs) {
    const bodySchema = rs.schema || {};
    const props = bodySchema.properties;
    if (props && typeof props === "object" && Object.keys(props).length) {
      for (const key of Object.keys(props)) {
        const sub = props[key] || {};
        out.push({ path: `params.body.${key}`, label: key, loc: "body", type: sub.type || "", required: false });
      }
    } else {
      out.push({ path: "params.body", label: "body", loc: "body", type: "object", required: !!rs.required });
    }
  }
  return out;
}

/* ----- auto-map: 선행 응답 필드명 ↔ 다음 입력 파라미터명 이름 매칭 ----- */
// 정확 일치(대소문자 무시) 우선 + 느슨한 포함 매칭 보조(예 id ↔ petId).
// 반환: [{from, to}] (와이어 키). 후보 없으면 [].
function normName(s) { return String(s || "").toLowerCase().replace(/[_\s-]/g, ""); }

function autoMapMappings(fromFields, toFields) {
  const mappings = [];
  const usedTo = new Set();
  // 1) 정확 일치 우선
  for (const ff of fromFields) {
    const fn = normName(ff.label);
    let hit = toFields.find(tf => !usedTo.has(tf.path) && normName(tf.label) === fn);
    if (hit) { mappings.push({ from: ff.path, to: hit.path }); usedTo.add(hit.path); }
  }
  // 2) 느슨한 포함 매칭(한쪽이 다른쪽 이름을 포함; 예 response.id ↔ target petId)
  for (const ff of fromFields) {
    if (mappings.some(m => m.from === ff.path)) continue;
    const fn = normName(ff.label);
    if (!fn) continue;
    let hit = toFields.find(tf => {
      if (usedTo.has(tf.path)) return false;
      const tn = normName(tf.label);
      if (!tn) return false;
      return tn.includes(fn) || fn.includes(tn);
    });
    if (hit) { mappings.push({ from: ff.path, to: hit.path }); usedTo.add(hit.path); }
  }
  return mappings;
}

// 엣지 양끝 노드의 operation 메타를 캐시/fetch 로 확보 후 콜백. (Drawflow id 기준)
async function resolveEdgeOps(edge) {
  const srcNode = editor.getNodeFromId(edge.source);
  const tgtNode = editor.getNodeFromId(edge.target);
  const srcOpId = srcNode && srcNode.data ? srcNode.data.operation_id : null;
  const tgtOpId = tgtNode && tgtNode.data ? tgtNode.data.operation_id : null;
  const [srcOp, tgtOp] = await Promise.all([ensureOperation(srcOpId), ensureOperation(tgtOpId)]);
  return { srcOp, tgtOp };
}

// 엣지 data_mapping 편집 (§7: 동적 값은 엣지 매핑으로만)
// operation 메타를 비동기로 확보한 뒤 응답/입력 필드 목록 + 클릭 삽입 + auto-map 제공.
function renderEdgeMapping(edge) {
  const wrap = document.getElementById("node-params");
  const srcNode = editor.getNodeFromId(edge.source);
  const tgtNode = editor.getNodeFromId(edge.target);
  const srcLabel = (srcNode && srcNode.data && srcNode.data.label) || edge.source;
  const tgtLabel = (tgtNode && tgtNode.data && tgtNode.data.label) || edge.target;

  // 비동기 메타 로딩 중 표시(현재 선택 엣지와 일치할 때만 최종 렌더)
  wrap.innerHTML = `<div class="params-head"><strong>엣지 매핑</strong></div>
    <p class="hint">${escapeHtml(srcLabel)} <span>→</span> ${escapeHtml(tgtLabel)}</p>
    <p class="muted">필드 목록 불러오는 중…</p>`;

  resolveEdgeOps(edge).then(({ srcOp, tgtOp }) => {
    // 로딩 중 선택이 바뀌었으면 무시
    if (!selectedEdge || selectedEdge.source !== edge.source || selectedEdge.target !== edge.target) return;
    drawEdgeMapping(edge, srcOp, tgtOp, srcLabel, tgtLabel);
  });
}

function drawEdgeMapping(edge, srcOp, tgtOp, srcLabel, tgtLabel) {
  const wrap = document.getElementById("node-params");
  const key = connKey(edge.source, edge.target);
  const mapping = edgeMappings[key] || [];

  const fromFields = responseFieldPaths(srcOp);  // [{path,label,type}]
  const toFields = paramInputPaths(tgtOp);       // [{path,label,loc,type,required}]
  const hasFrom = fromFields.length > 0;
  const hasTo = toFields.length > 0;

  let html = `<div class="params-head"><strong>엣지 매핑</strong></div>
    <p class="hint">${escapeHtml(srcLabel)} <span>→</span> ${escapeHtml(tgtLabel)}</p>`;

  // 자동 매핑 버튼(양쪽 필드 모두 있을 때만 의미 있음)
  if (hasFrom && hasTo) {
    html += `<div class="map-actions">
      <button class="small primary" onclick="autoMapEdge()">⚡ 자동 매핑</button>
    </div>`;
  }

  // 필드 팔레트(클릭 삽입)
  if (hasFrom || hasTo) {
    html += `<div class="map-fields">`;
    if (hasFrom) {
      html += `<div class="map-field-col"><div class="map-field-title">선행 응답 필드 (from)</div><div class="chips">` +
        fromFields.map(f => `<button type="button" class="chip chip-from"
            data-path="${escapeAttr(f.path)}"
            title="${escapeAttr(f.path)}">${escapeHtml(f.label)}<small>${escapeHtml(f.type || "")}</small></button>`).join("") +
        `</div></div>`;
    } else {
      html += `<div class="map-field-col"><div class="map-field-title">선행 응답 필드 (from)</div>
        <p class="muted small">응답 스키마 없음 — 직접 입력하세요</p></div>`;
    }
    if (hasTo) {
      html += `<div class="map-field-col"><div class="map-field-title">다음 입력 파라미터 (to)</div><div class="chips">` +
        toFields.map(f => `<button type="button" class="chip chip-to"
            data-path="${escapeAttr(f.path)}"
            title="${escapeAttr(f.path)}">${escapeHtml(f.label)}${f.required ? ' <em>*</em>' : ''}<small>${escapeHtml(f.loc || "")}</small></button>`).join("") +
        `</div></div>`;
    } else {
      html += `<div class="map-field-col"><div class="map-field-title">다음 입력 파라미터 (to)</div>
        <p class="muted small">입력 스키마 없음 — 직접 입력하세요</p></div>`;
    }
    html += `</div>
      <p class="hint">필드를 클릭하면 활성(마지막 클릭) 행의 from/to 에 경로가 자동 입력됩니다.</p>`;
  } else {
    // fallback: 양쪽 스키마 모두 없음 → 기존 수동 입력 유지
    html += `<p class="hint">응답/입력 스키마 없음 — 직접 입력하세요.
      from: 선행 노드 출력 경로(예 <code>$.id</code>) ·
      to: <code>params.path.&lt;k&gt;</code> / <code>params.query.&lt;k&gt;</code> /
      <code>params.header.&lt;k&gt;</code> / <code>params.body</code>[.&lt;k&gt;]</p>`;
  }

  html += `<div id="mapping-rows">`;
  mapping.forEach((m, i) => { html += mappingRow(m.from, m.to, i); });
  html += `</div>
    <button class="small" onclick="addMappingRow()">+ 매핑 추가</button>`;
  wrap.innerHTML = html;

  // 칩 클릭 → 활성 행의 from/to 에 삽입(행 없으면 새로 생성)
  wrap.querySelectorAll(".chip-from").forEach(btn => {
    btn.addEventListener("click", () => insertPathIntoActiveRow("from", btn.dataset.path));
  });
  wrap.querySelectorAll(".chip-to").forEach(btn => {
    btn.addEventListener("click", () => insertPathIntoActiveRow("to", btn.dataset.path));
  });
  // 행 input 포커스 추적 → 활성 행 표시
  wrap.querySelectorAll(".mapping-row input").forEach(inp => {
    inp.addEventListener("focus", () => setActiveMappingRow(inp.closest(".mapping-row")));
  });
}

let activeMappingRowEl = null;
function setActiveMappingRow(rowEl) {
  if (!rowEl) return;
  document.querySelectorAll("#mapping-rows .mapping-row").forEach(r => r.classList.remove("active-row"));
  rowEl.classList.add("active-row");
  activeMappingRowEl = rowEl;
}

// 칩 클릭 시 활성 행(없으면 빈 행 우선, 그래도 없으면 새 행)의 from/to 칸에 경로 삽입.
function insertPathIntoActiveRow(which, path) {
  let rows = Array.from(document.querySelectorAll("#mapping-rows .mapping-row"));
  let row = activeMappingRowEl && document.body.contains(activeMappingRowEl) ? activeMappingRowEl : null;
  if (!row) {
    // 해당 칸이 비어있는 첫 행 선호
    row = rows.find(r => !r.querySelector(which === "from" ? ".map-from" : ".map-to").value.trim());
  }
  if (!row) {
    persistMapping();
    addMappingRow();                       // 새 행 추가 후 다시 조회
    rows = Array.from(document.querySelectorAll("#mapping-rows .mapping-row"));
    row = rows[rows.length - 1];
  }
  if (!row) return;
  const input = row.querySelector(which === "from" ? ".map-from" : ".map-to");
  input.value = path;
  setActiveMappingRow(row);
  persistMapping();
}

function mappingRow(from, to, i) {
  return `<div class="mapping-row" data-i="${i}">
    <input class="map-from" placeholder="$.id" value="${escapeHtml(from || "")}" />
    <span>→</span>
    <input class="map-to" placeholder="params.path.petId" value="${escapeHtml(to || "")}" />
    <button class="danger small" onclick="removeMappingRow(${i})">×</button>
  </div>`;
}

// "자동 매핑" 버튼: 선행 응답 ↔ 다음 입력 이름 매칭으로 행을 채워 제시(기존 행은 보존,
// 중복 from/to 는 건너뜀). 사용자가 이후 수정/삭제/추가 가능.
function autoMapEdge() {
  if (!selectedEdge) return;
  persistMapping();
  resolveEdgeOps(selectedEdge).then(({ srcOp, tgtOp }) => {
    if (!selectedEdge) return;
    const proposed = autoMapMappings(responseFieldPaths(srcOp), paramInputPaths(tgtOp));
    const key = connKey(selectedEdge.source, selectedEdge.target);
    const existing = edgeMappings[key] || [];
    const haveFrom = new Set(existing.map(m => m.from));
    const haveTo = new Set(existing.map(m => m.to));
    // 의미 없는 빈 행 제거 후 병합
    const merged = existing.filter(m => m.from || m.to);
    for (const m of proposed) {
      if (haveFrom.has(m.from) || haveTo.has(m.to)) continue;
      merged.push(m);
      haveFrom.add(m.from); haveTo.add(m.to);
    }
    edgeMappings[key] = merged;
    renderEdgeMapping(selectedEdge);
  });
}

function collectMapping() {
  const rows = document.querySelectorAll("#mapping-rows .mapping-row");
  const out = [];
  rows.forEach(r => {
    const from = r.querySelector(".map-from").value.trim();
    const to = r.querySelector(".map-to").value.trim();
    if (from || to) out.push({ from, to }); // 와이어 키 from/to 고정
  });
  return out;
}

function persistMapping() {
  if (!selectedEdge) return;
  edgeMappings[connKey(selectedEdge.source, selectedEdge.target)] = collectMapping();
}

function addMappingRow() {
  persistMapping();
  const m = edgeMappings[connKey(selectedEdge.source, selectedEdge.target)] || [];
  m.push({ from: "", to: "" });
  edgeMappings[connKey(selectedEdge.source, selectedEdge.target)] = m;
  renderEdgeMapping(selectedEdge);
}

function removeMappingRow(i) {
  persistMapping();
  const k = connKey(selectedEdge.source, selectedEdge.target);
  const m = edgeMappings[k] || [];
  m.splice(i, 1);
  edgeMappings[k] = m;
  renderEdgeMapping(selectedEdge);
}

// 입력 변경 시 즉시 반영 (blur/change 위임)
document.addEventListener("change", (e) => {
  if (e.target.closest && e.target.closest(".mapping-row")) persistMapping();
});

/* ---- 실행 결과 렌더 ---- */
function renderExecution(result) {
  const summary = document.getElementById("exec-summary");
  const logs = document.getElementById("exec-logs");
  const resultWrap = document.getElementById("exec-result-wrap");
  const resultPre = document.getElementById("exec-result");

  const cls = result.status === "success" ? "ok" : (result.status === "failed" ? "fail" : "run");
  summary.className = `exec-summary ${cls}`;
  summary.innerHTML = `실행 #${result.execution_id} ·
    <strong>${escapeHtml(result.status)}</strong> ·
    <span class="muted">${escapeHtml(result.started_at || "")} → ${escapeHtml(result.finished_at || "")}</span>`;

  logs.innerHTML = (result.logs || []).map(l => `
    <li class="log-item log-${escapeHtml(l.status)}">
      <div class="log-head">
        <span class="seq">#${l.seq}</span>
        <span class="node-key">${escapeHtml(l.node_key)}</span>
        <span class="badge ${escapeHtml(l.status)}">${escapeHtml(l.status)}</span>
        <span class="muted ts">${escapeHtml(l.timestamp || "")}</span>
      </div>
      ${l.error ? `<div class="log-error">${escapeHtml(l.error)}</div>` : ""}
      <details><summary>input / output</summary>
        <pre class="json-block">input: ${escapeHtml(jsonStr(l.input))}
output: ${escapeHtml(jsonStr(l.output))}</pre>
      </details>
    </li>`).join("");

  if (result.result !== undefined && result.result !== null) {
    resultWrap.hidden = false;
    resultPre.textContent = jsonStr(result.result);
  } else {
    resultWrap.hidden = true;
  }

  applyNodeStatuses(result.logs);
}

/* =============================================================== 팔레트 ===== */
// 업로드 결과의 operations(OperationOut[])로 팔레트 렌더
function renderOperations(operations) {
  const palette = document.getElementById("operation-palette");
  if (!operations || !operations.length) {
    palette.innerHTML = '<li class="muted">오퍼레이션이 없습니다.</li>';
    return;
  }
  palette.innerHTML = operations.map(op => `
    <li class="op-item" draggable="true"
        data-op='${escapeAttr(JSON.stringify(op))}'>
      <span class="method method-${escapeHtml((op.method || "").toLowerCase())}">${escapeHtml(op.method)}</span>
      <span class="op-path">${escapeHtml(op.path)}</span>
      <span class="op-summary muted">${escapeHtml(op.summary || "")}</span>
    </li>`).join("");

  palette.querySelectorAll(".op-item").forEach(li => {
    li.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("application/json", li.getAttribute("data-op"));
    });
  });
}

/* 스펙 업로드 핸들러 (spec_upload.html 의 폼에서 호출) */
async function uploadSpecFile(ev) {
  ev.preventDefault();
  const form = ev.target;
  const status = document.getElementById("spec-status");
  status.textContent = "업로드 중…";
  const fd = new FormData();
  fd.append("file", form.file.files[0]); // multipart field name = "file"
  try {
    const res = await fetch(`${API}/specs/upload`, { method: "POST", body: fd });
    if (!res.ok) throw new Error(`업로드 실패 (${res.status})`);
    handleSpecResult(await res.json());
  } catch (e) {
    status.textContent = e.message;
    status.className = "error-msg";
  }
  return false;
}

async function uploadSpecUrl(ev) {
  ev.preventDefault();
  const form = ev.target;
  const status = document.getElementById("spec-status");
  status.textContent = "가져오는 중…";
  const body = { url: form.url.value.trim(), name: form.name.value.trim() || null };
  try {
    const res = await fetch(`${API}/specs/from-url`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`가져오기 실패 (${res.status})`);
    handleSpecResult(await res.json());
  } catch (e) {
    status.textContent = e.message;
    status.className = "error-msg";
  }
  return false;
}

// SpecUploadResult = {spec, operation_count, operations, warnings}
function handleSpecResult(result) {
  const status = document.getElementById("spec-status");
  status.className = "muted";
  status.textContent =
    `${result.spec.name} · operations ${result.operation_count}개`;
  const warn = document.getElementById("spec-warnings");
  warn.textContent = (result.warnings && result.warnings.length)
    ? "경고: " + result.warnings.join("; ") : "";
  cacheOperations(result.operations);
  renderOperations(result.operations);
}

/* ================================================================= utils ==== */
function jsonStr(v) {
  if (v === undefined || v === null) return "null";
  try { return JSON.stringify(v, null, 2); } catch { return String(v); }
}
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}
function escapeAttr(s) {
  return String(s).replace(/'/g, "&#39;").replace(/"/g, "&quot;");
}

// 전역 노출 (인라인 onclick 에서 호출)
window.initEditor = initEditor;
window.addStartNode = addStartNode;
window.addEndNode = addEndNode;
window.saveWorkflow = saveWorkflow;
window.openRunDialog = openRunDialog;
window.closeRunDialog = closeRunDialog;
window.runWorkflow = runWorkflow;
window.switchTab = switchTab;
window.addMappingRow = addMappingRow;
window.removeMappingRow = removeMappingRow;
window.autoMapEdge = autoMapEdge;
window.uploadSpecFile = uploadSpecFile;
window.uploadSpecUrl = uploadSpecUrl;
