# 04 — Frontend Status (mcp-provider)

> 소유자: frontend-engineer. 계약 §4(그래프 JSON) / §5(REST)를 정본으로 삼아 구현.
> 백엔드/엔진 status 문서(02/03)는 아직 미생성이라 **계약 01번을 단일 기준**으로 사용했다.

## 산출 파일 (계약 §9 소유 범위)

| 파일 | 내용 |
|------|------|
| `templates/base.html` | 레이아웃, htmx 1.9.12 + Drawflow 0.0.59 CDN 로드, `/static/*` 링크 |
| `templates/index.html` | 워크플로우 목록(`GET /api/workflows`) + 생성(`POST /api/workflows`) + 삭제 |
| `templates/editor.html` | 캔버스 에디터: 좌(스펙/팔레트)·중(Drawflow)·우(속성/로그) 3분할, 실행 다이얼로그 |
| `templates/partials/spec_upload.html` | 파일/URL 업로드 폼 |
| `templates/partials/operation_list.html` | operation 팔레트 컨테이너(JS가 채움) |
| `templates/partials/node_params.html` | 속성 패널 기본 상태 |
| `templates/partials/execution_log.html` | 실행 로그 컨테이너 |
| `static/canvas.js` | Drawflow 초기화 + `toContractGraph`/`fromContractGraph` + 전체 에디터 로직 |
| `static/style.css` | 다크 테마 스타일 |

## 사용한 REST 엔드포인트 (계약 §5)

| 호출 위치 | Method | Path | 요청 | 응답 사용 필드 |
|-----------|--------|------|------|----------------|
| index | GET | `/api/workflows` | — | `list[WorkflowSummary]`: id, name, description, mcp_exposed, updated_at |
| index | POST | `/api/workflows` | `{name, description?}` (WorkflowCreateRequest) | `WorkflowDetail.id` → `/editor/{id}` 이동 |
| index | DELETE | `/api/workflows/{id}` | — | 200 확인만 |
| editor(load) | GET | `/api/workflows/{id}` | — | `WorkflowDetail`: name, nodes[], edges[] |
| editor(save) | PUT | `/api/workflows/{id}` | `{nodes, edges}` (WorkflowSaveRequest) | 200 + WorkflowDetail |
| editor(run) | POST | `/api/workflows/{id}/run` | `{initial_input, auth}` (RunRequest) | `ExecutionResult`: execution_id, status, started_at, finished_at, result, logs[] |
| spec_upload | POST | `/api/specs/upload` | multipart, field `file` | `SpecUploadResult`: spec, operation_count, operations[], warnings[] |
| spec_upload | POST | `/api/specs/from-url` | `{url, name?}` (SpecFromUrlRequest) | `SpecUploadResult` |

`GET /api/specs`, `GET /api/specs/{id}/operations`, `GET /api/executions/{id}`,
SSE stream, expose 토글은 **미사용**(1차 MVP 흐름엔 불필요). 업로드 응답에 operations가
포함되므로 별도 operations 조회 불필요.

## Drawflow 버전 / CDN

- htmx `1.9.12` (unpkg, SRI 포함)
- Drawflow `0.0.59` (jsdelivr) — CSS + JS
- 빌드 도구 없음. 전부 CDN/정적.

## 캔버스 직렬화 shape (계약 §4 정확 일치)

`toContractGraph(editor.export())` → PUT body `{nodes, edges}`.
키·중첩·타입은 §4와 100% 일치. 실제 export 예시 (start → GET /pet/{petId} → end):

```json
{
  "nodes": [
    {
      "id": "node_0",
      "type": "start",
      "label": "start",
      "operation_id": null,
      "params": { "path": {}, "query": {}, "header": {}, "body": null },
      "position": { "x": 60, "y": 80 }
    },
    {
      "id": "node_1",
      "type": "api_call",
      "label": "GET /pet/{petId}",
      "operation_id": 42,
      "params": {
        "path": { "petId": "10" },
        "query": {},
        "header": {},
        "body": null
      },
      "position": { "x": 320, "y": 80 }
    },
    {
      "id": "node_2",
      "type": "end",
      "label": "end",
      "operation_id": null,
      "params": { "path": {}, "query": {}, "header": {}, "body": null },
      "position": { "x": 640, "y": 80 }
    }
  ],
  "edges": [
    {
      "id": "edge_0",
      "source": "node_0",
      "target": "node_1",
      "data_mapping": [
        { "from": "$.petId", "to": "params.path.petId" }
      ]
    },
    {
      "id": "edge_1",
      "source": "node_1",
      "target": "node_2",
      "data_mapping": []
    }
  ]
}
```

- `data_mapping` 항목의 와이어 키는 문자열 `"from"`, `"to"` (§5.0 / §11-2 준수).
- `operation_id`는 api_call일 때 `OperationOut.id`(정수), 그 외 `null`.
- `params.body`는 object 또는 `null`.
- 노드/엣지 id는 문자열 `node_N`/`edge_N` (Drawflow 로컬 키, §0-7).
- `fromContractGraph`는 위 shape을 받아 Drawflow import 객체로 역변환(노드 배치 +
  엣지 연결 + data_mapping 복원). round-trip 키 보존.

### Drawflow ↔ 계약 매핑 메모
- Drawflow는 자체 숫자 노드 id를 쓴다. 계약 `node.id`(문자열)는 `node.data.node_key`에
  보존하고, 직렬화 시 숫자 id→node_key 매핑 테이블로 변환한다.
- 엣지 `data_mapping`은 Drawflow 연결 객체에 저장할 자리가 없어, 모듈 전역
  `edgeMappings["{src}->{tgt}"]` 맵에 보관하고 직렬화/역직렬화 시 합류시킨다.
- 노드 종류별 포트: start = 입력0/출력1, end = 입력1/출력0, api_call/transform = 입력1/출력1.

## 데이터 매핑 UI (§7 준수)
- 동적 값은 **엣지 매핑으로만** 전달. 노드 params는 정적 기본값 전용(폼에 명시).
- 엣지 클릭 → 우측 "속성" 패널에 `{from, to}` 행 편집기. from은 `$.id` 같은 JSONPath
  부분집합, to는 `params.path.<k>` / `params.query.<k>` / `params.header.<k>` /
  `params.body`[.<k>] 형식 안내.

## 파라미터 폼
- `OperationOut.params_schema`(§3 평탄 구조: path/query/header 배열)를 폼 필드로 렌더.
  `enum` 있으면 select, 아니면 text. `required`는 `*` 표시.
- `request_schema` 존재 시 body를 JSON textarea로 입력(파싱 검증 포함).

## 백엔드에 필요한 라우트 (Jinja2 페이지 렌더 — 계약에 미명시, 확인 필요)
계약 §5는 `/api/*` JSON만 정의한다. 페이지 렌더 라우트는 backend-engineer가
`app.py`에서 마운트해야 한다. 프론트가 가정한 라우트:
- `GET /` → `index.html` 렌더
- `GET /editor/{id}` → `editor.html` 렌더, 템플릿 컨텍스트에 `workflow_id` 주입
  (editor.html은 `<meta name="workflow-id" content="{{ workflow_id }}">`로 읽음)
- `StaticFiles` 마운트 경로 `/static`, `Jinja2Templates(directory="templates")`

## 미해결 의존 / 확인 필요 (backend·engine 경계)
1. **페이지 렌더 라우트**: 위 `/`, `/editor/{id}` 및 컨텍스트 변수명 `workflow_id`를
   backend가 그대로 제공하는지 확인 필요. (다르면 editor.html meta 1줄만 수정)
2. **운영체제 응답 round-trip**: 워크플로우 로드 시 `WorkflowDetail.nodes[].operation`
   원본(OperationOut)은 응답에 없다. 로드된 api_call 노드의 파라미터 폼은
   `operation`이 null이라 **스키마 기반 필드를 못 그린다**(이미 채워진 params 값은
   보존·저장됨). 개선하려면 backend가 node에 operation 메타를 포함하거나,
   프론트가 `GET /api/specs/{id}/operations`로 재조회해야 함 → 1차 범위에서는
   "신규 배치 시에만 폼 렌더, 로드 후엔 값 보존" 으로 한정. backend 확인 후 결정.
3. **ExecutionResult.result 타입**: §5는 `Optional[Any]`. 프론트는 JSON.stringify로
   표시하므로 모든 타입 수용. 문제 없음.
4. **노드 실패 = HTTP 200 + status:"failed"** (§11-3): 프론트는 `res.ok`가 아니라
   `ExecutionResult.status`/`NodeLog.error`로 실패 판정. 준수함.

## 알려진 한계
- 로드된(저장 후 재진입) api_call 노드는 operation 스키마가 없어 동적 파라미터 폼
  필드를 재구성하지 못함(위 #2). 저장된 params 값 자체는 유지·재저장됨.
- SSE 실시간 진행 표시 미구현(계약상 선택, 1차 우선순위 낮음). 동기 run 응답만 사용.
- MCP expose 토글 UI 미구현(계약 우선순위 낮음).
- 순차 실행 전제(§0-3)에 맞춰 노드 포트를 1입력/1출력으로 단순화. 분기 UI 없음.
- 클라이언트 측 그래프 검증(순환 등) 없음 — 엔진이 검증(§6)하고 실패 로그로 표시.
```

---

## 기능추가 이력

### [기능추가] 엣지 data_mapping UX 개선 (응답 필드 클릭 삽입 + auto-map)
소유 파일만 수정: `static/canvas.js`, `static/style.css`, `templates/partials/node_params.html`.
backend/engine 미수정. 빌드 도구 없음(htmx + Drawflow CDN/정적).

**1) operation 메타 캐시 + 신규 엔드포인트 사용**
- `operationCache`(by `operation_id`=OperationOut.id), `operationPending`(중복 fetch 방지) 추가.
- 채우는 경로: ① 스펙 업로드 응답 `operations[]`(`cacheOperations`), ② 팔레트 드롭(`addApiNode`),
  ③ **`GET /api/operations/{operation_id}`** lazy fetch(`ensureOperation`) — `OperationOut`(§5.2) 반환.
  이 엔드포인트는 backend `routers/operations.py`에 이미 존재함(확인 완료).
- 효과: **저장된 워크플로우 재진입 시에도** 각 노드의 응답/입력 필드를 복원. 기존 "알려진 한계"
  (로드 후 api_call 노드 파라미터 폼 재구성 불가, 위 미해결의존 #2)도 이 캐시로 개선 —
  `renderNodeParams`가 `d.operation`이 null이면 캐시/`ensureOperation`으로 메타를 복원해 폼 재렌더.

**2) 스키마 → 필드 경로 추출**
- `responseFieldPaths(op)`: `response_schema`에서 200(없으면 첫 2xx/`default`) 선택 →
  `type:object` → `$.<key>`, `type:array & items:object` → `$[0].<key>`(엔진 JSONPath 부분집합 `[i]` 사용,
  `[*]` 미사용 — 계약 §4). 한 단계 중첩까지. 추출 불가 시 `[]`.
- `paramInputPaths(op)`: `params_schema.path/query/header[].name` → `params.<loc>.<name>`,
  requestBody(`request_schema.schema.properties` 키) → `params.body.<key>`(properties 없으면 `params.body`).

**3) 매핑 편집기 UI**
- 엣지 선택/연결 시 `resolveEdgeOps`로 양끝 노드 operation 메타를 비동기 확보 후 `drawEdgeMapping` 렌더.
- 선행 응답 필드 칩(from), 다음 입력 파라미터 칩(to) 표시. 칩 클릭 → 활성(마지막 포커스/클릭) 매핑 행의
  from/to 칸에 경로 자동 입력(`insertPathIntoActiveRow`). 행 없으면 새 행 생성.

**4) 자동 채움 (auto-map)**
- `autoMapMappings(fromFields,toFields)`: 이름 매칭. ① 정확 일치(대소문자·`_`·공백·`-` 무시) 우선,
  ② 느슨한 포함 매칭 보조(예 응답 `id` ↔ target path `petId`). 각 to는 1회만 사용.
- 트리거: ① 엣지 신규 연결(`connectionCreated`) 시 미리 채움(기존 매핑 있으면 건드리지 않음),
  ② 편집기의 "⚡ 자동 매핑" 버튼(`autoMapEdge`, 기존 행 보존·중복 from/to 스킵).
- 사용자가 행 수정/삭제/추가 가능.

**5) Fallback (스키마 없음)**
- `response_schema`/`params_schema`가 비어 필드 추출 불가 시 칩 대신
  "스키마 없음 — 직접 입력하세요" 힌트 + 기존 빈 텍스트 직접 입력 행 유지(기존 동작 보존).

**경계/계약 보존**
- `edgeMappings` 구조 불변(`[{from,to}]`), `collectMapping`/`persistMapping`/`toContractGraph`/
  `fromContractGraph` 미변경 → 저장 와이어 키 문자열 `"from"`/`"to"` 100% 유지(round-trip 보존, §11-2).
  Node 직렬화 shape(§4)도 불변. 칩/auto-map은 입력 `.value`를 채울 뿐 직렬화 경로를 바꾸지 않음.

**검증**
- `node --check static/canvas.js` 통과.
- 추출/auto-map 로직 단위 검증(Node 스니펫 13건 통과): object→`$.id`,`$.name`,`$.category` /
  array→`$[0].id`,`$[0].status` / no-schema→`[]`(fallback) / 201·default fallback /
  param paths(path,query,header,body.props) / body-no-props→`params.body` /
  auto-map 정확(name↔name)+느슨(id↔petId) / 무매칭→`[]` / 정확 일치가 느슨보다 우선.
- 와이어 키 불변 검증: 직렬화 결과 키 집합 = {"from","to"}.
