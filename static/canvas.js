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

let editor = null;          // Drawflow 인스턴스
let currentWorkflowId = null;
let selectedNodeId = null;  // Drawflow 숫자 id
let selectedEdge = null;    // {source, target} Drawflow ids
let nodeSeq = 0;            // node_key 발급용 카운터
let edgeSeq = 0;            // edge_key 발급용 카운터
// Drawflow 숫자 노드 id -> 계약 node_key("node_N") 매핑 보존
const edgeMappings = {};    // "df_src->df_tgt" -> [ {from,to} ]

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
  const container = document.getElementById("drawflow");
  editor = new Drawflow(container);
  editor.reroute = true;
  editor.start();

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
  return `<div class="df-node"><div class="df-node-title">${escapeHtml(label)}</div>` +
         (sub ? `<div class="df-node-sub">${escapeHtml(sub)}</div>` : "") + `</div>`;
}

function addApiNode(operation, posX, posY) {
  if (operation && operation.id != null) operationCache[operation.id] = operation;
  const label = `${operation.method} ${operation.path}`;
  const data = {
    node_key: makeNodeKey(),
    ctype: "api_call",
    label: label,
    operation_id: operation.id,            // OperationOut.id (= operations.id DB PK)
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
    const wf = await res.json(); // WorkflowDetail {id,name,description,...,nodes,edges}
    document.getElementById("wf-title").textContent = wf.name || `워크플로우 #${id}`;
    fromContractGraph({ nodes: wf.nodes || [], edges: wf.edges || [] });
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
function openRunDialog() {
  document.getElementById("run-error").textContent = "";
  document.getElementById("run-dialog").showModal();
}
function closeRunDialog() {
  document.getElementById("run-dialog").close();
}

async function runWorkflow(ev) {
  ev.preventDefault();
  const errEl = document.getElementById("run-error");
  errEl.textContent = "";
  let initialInput, auth;
  try {
    initialInput = JSON.parse(document.getElementById("run-initial-input").value || "{}");
    auth = JSON.parse(document.getElementById("run-auth").value || "{}");
  } catch (e) {
    errEl.textContent = "JSON 형식 오류: " + e.message;
    return false;
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
