---
name: htmx-canvas-ui
description: Jinja2 서버 렌더링 + htmx 부분 갱신 + Drawflow 드래그앤드롭 노드/엣지 캔버스로 워크플로우 빌더 웹 UI를 구현하는 방법. 빌드 도구 없이 CDN/정적 파일로 동작. "프론트엔드 UI", "htmx", "Jinja2 템플릿", "드래그앤드롭 캔버스", "노드 에디터", "그래프 UI", "워크플로우 빌더 화면" 작업 시 반드시 사용. frontend-engineer 전용.
---

# htmx + Jinja2 + Drawflow 캔버스 UI

서버 렌더링 페이지 위에 드래그앤드롭 노드 캔버스를 얹는다. 백엔드 응답 shape은 `_workspace/02_backend_status.md`·`03_engine_status.md`를 정확히 따른다.

## 왜 추측 금지인가
경계면 버그의 1순위 원인은 프론트가 백엔드 응답 shape을 추측해서 만드는 것이다. 엔드포인트 응답·노드 스키마가 불확실하면 추측하지 말고 backend/mcp-engineer에 SendMessage로 확인한다.

## 기술 구성 (빌드 도구 없음)
- **Jinja2**: FastAPI `Jinja2Templates`로 서버 렌더링. 레이아웃 상속(`base.html`).
- **htmx**: CDN 스크립트. 폼 제출·부분 갱신·폴링/SSE. `hx-post`, `hx-get`, `hx-target`, `hx-swap`.
- **Drawflow**: 바닐라 JS 노드 에디터(CDN/정적). 노드 배치·엣지 연결·그래프 JSON export/import. 빌드 불필요.
- 정적 자산은 FastAPI `StaticFiles`로 서빙.

## 화면 구성
```
templates/
├── base.html              # 레이아웃, htmx/Drawflow CDN 로드
├── index.html             # 워크플로우 목록
├── editor.html            # 캔버스 에디터 (Drawflow 컨테이너 + 파라미터 패널)
├── partials/
│   ├── spec_upload.html   # 파일/URL 업로드 폼 (htmx)
│   ├── operation_list.html# 스펙 operation → 드래그 가능한 노드 팔레트
│   ├── node_params.html   # 선택 노드 파라미터 편집 폼 (htmx swap)
│   └── execution_log.html # 실행 로그/상태 (SSE 또는 폴링)
static/
├── canvas.js              # Drawflow 초기화, 그래프 ↔ 백엔드 JSON 변환
└── style.css
```

## 캔버스 ↔ 백엔드 직렬화 (가장 중요)
Drawflow 내부 export 포맷을 그대로 저장하지 말고, **계약의 정규 노드/엣지 JSON**(`mcp-provider-architecture`)으로 변환해서 저장한다:
```js
// canvas.js: Drawflow export → 계약 shape
function toContractGraph(drawflowExport) {
  // node.data에 operation_id/params/label 보관
  // {nodes:[{id,type,label,operation_id,params,position}], edges:[{id,source,target,data_mapping}]}
}
function fromContractGraph(graph) { /* 역변환하여 Drawflow에 import */ }
```
- 저장: `PUT /api/workflows/{id}`에 `toContractGraph(...)` 결과를 `hx-post`/fetch로 전송.
- 로드: `GET /api/workflows/{id}` → `fromContractGraph(...)` → Drawflow import.
- 이 변환 shape이 mcp-engineer 노드/엣지 스키마와 어긋나면 실행이 깨진다 — qa-integrator가 이 경계를 검증한다.

## htmx 패턴
- 스펙 업로드: `<form hx-post="/api/specs/upload" hx-target="#operations" hx-encoding="multipart/form-data">`.
- operation 팔레트: 업로드 후 부분 갱신으로 드래그 가능한 노드 목록 표시.
- 노드 파라미터: 노드 클릭 시 `hx-get="/partials/node_params?op={id}"`로 폼 로드, 값 입력 후 캔버스 노드 data에 반영.
- 실행: `hx-post="/api/workflows/{id}/run"` → 실행 로그 영역을 SSE(`hx-ext="sse"`) 또는 폴링(`hx-trigger="every 1s"`)으로 갱신.

## 파라미터 폼 렌더링
operation의 `params_schema`(path/query/header/body)를 폼 필드로 렌더링. 다른 노드 출력 참조는 `{{node_x.output.field}}` 또는 edge 매핑 UI로 지정 (엔진이 택한 방식 따름).

## 출력
완료 시 `_workspace/04_frontend_status.md`에 사용한 엔드포인트·캔버스 직렬화 shape·미해결 의존을 기록한다.
