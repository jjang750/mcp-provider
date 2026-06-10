# 02 — Backend Status (mcp-provider)

> 작성: backend-engineer. 계약(`01_architect_contracts.md`) §2/§4/§5/§6/§9를 그대로 구현.
> 대상: Python 3.11. 단일 FastAPI 앱. SQLite 단일 파일 `mcp_provider.db`(repo 루트, 런타임 생성).
> **상태: 1차 MVP 필수 엔드포인트 전부 구현 + 실제 기동/스모크 통과. engine 런타임 결합 LIVE 확인됨.**

---

## 0. 실행 방법

```bash
pip install -r requirements.txt
# repo 루트(mcp-provider/)에서:
uvicorn backend.app:app --reload --port 8000
```

- 앱 import 경로: `backend.app:app` (패키지 import).
- 시작 시 lifespan이 `db.init_db()`로 §2 DDL을 idempotent 실행 → `mcp_provider.db` 생성.
- DB 경로 override: 환경변수 `MCP_PROVIDER_DB` (테스트용).
- Swagger UI: `/docs`. 헬스체크: `GET /api/health` → `{"status":"ok"}`.
- 루트 `GET /`: `templates/index.html` 있으면 렌더, 없으면 `{"app":"mcp-provider",...}` JSON (frontend 미완성에도 500 안 남).

---

## 1. 생성/소유 파일 (계약 §9)

| 파일 | 내용 |
|------|------|
| `backend/app.py` | FastAPI 앱, lifespan(DDL 마이그레이션), CORS, 라우터 등록, StaticFiles(`/static`)·Jinja2(`templates/`) 마운트. `static/`·`templates/` 없으면 자동 생성 후 마운트 |
| `backend/db.py` | sqlite3 연결(`check_same_thread=False`, Row factory, FK on), §2 DDL `executescript`, `get_db` 의존성 |
| `backend/models.py` | §5.0/§5.2 Pydantic v2 모델 **정본 전부** |
| `backend/engine_bridge.py` | `engine` 패키지 lazy import 래퍼(§6). 미구현 시 `EngineUnavailableError`→503. ExecutionResult/ParseResult 정규화 |
| `backend/repositories/_util.py` | `utc_now()`(ISO-8601 UTC), `dumps/loads`(JSON TEXT 직렬화) |
| `backend/repositories/specs.py` | specs + operations CRUD, 파서 dict→operations INSERT, Row→`OperationOut` |
| `backend/repositories/workflows.py` | workflows + nodes + edges CRUD, 그래프 load/save(트랜잭션 교체), `WorkflowGraph` 로드 |
| `backend/repositories/executions.py` | executions + execution_logs CRUD, `ExecutionResult` 영속화/조회 |
| `backend/routers/specs.py` | `/api/specs*` (upload/from-url/list/operations) |
| `backend/routers/workflows.py` | `/api/workflows*` (list/create/get/put/delete/run) |
| `backend/routers/executions.py` | `/api/executions/{id}` |
| `requirements.txt` | 전체 의존성(engine·MCP 포함, §10) |

---

## 2. 구현 엔드포인트 + 실제 요청/응답 shape

베이스 prefix `/api`. **모든 JSON 컬럼은 응답에서 파싱된 객체로 노출**(문자열 아님).

### specs

| Method | Path | 요청 | 응답(200) |
|--------|------|------|-----------|
| POST | `/api/specs/upload` | multipart, field명 **`file`** (.json/.yaml/.yml, ≤10MB, UTF-8) | `SpecUploadResult` |
| POST | `/api/specs/from-url` | `{"url": "...", "name": null}` (SSRF 가드: 사설/loopback/link-local 차단, http(s)만, 15s 타임아웃, 리다이렉트≤3) | `SpecUploadResult` |
| GET | `/api/specs` | — | `list[SpecSummary]` (id 내림차순) |
| GET | `/api/specs/{spec_id}/operations` | — | `list[OperationOut]` (미존재 spec → 404) |

`SpecUploadResult` 실제 shape:
```json
{
  "spec": {"id":1,"name":"s.json","source_type":"file","spec_version":"3.0.0","created_at":"2026-...Z"},
  "operation_count": 1,
  "operations": [ /* OperationOut[] */ ],
  "warnings": []
}
```
`OperationOut` 실제 shape:
```json
{
  "id": 1, "spec_id": 1, "operation_id": "getPet",
  "method": "GET", "path": "/pet/{petId}", "base_url": "https://api.x.com", "summary": null,
  "params_schema": {"path":[{"name":"petId","type":"integer","required":true,...}],"query":[],"header":[]},
  "request_schema": null, "response_schema": {...}|null, "auth": {...}|null
}
```
> `operations[].id`(= operations 테이블 PK)가 곧 **노드의 `operation_id`로 쓰는 정수**다 (§5.2). 프론트는 노드 생성 시 이 값을 `Node.operation_id`에 넣어라.
> 파서가 준 `base_url`은 op에 `base_url`이 비어있으면 백엔드가 채워 넣는다.

### workflows

| Method | Path | 요청 | 응답(200) |
|--------|------|------|-----------|
| GET | `/api/workflows` | — | `list[WorkflowSummary]` |
| POST | `/api/workflows` | `WorkflowCreateRequest` `{"name","description?"}` | `WorkflowDetail` (빈 nodes/edges) |
| GET | `/api/workflows/{id}` | — | `WorkflowDetail` (미존재 404) |
| PUT | `/api/workflows/{id}` | `WorkflowSaveRequest` `{"nodes":[Node],"edges":[Edge],"name?","description?"}` | `WorkflowDetail` |
| DELETE | `/api/workflows/{id}` | — | `{"deleted": true}` (미존재 404) |
| POST | `/api/workflows/{id}/run` | `RunRequest` `{"initial_input":{},"auth":{}}` | `ExecutionResult` (동기) |

`WorkflowDetail`:
```json
{
  "id":1,"name":"WF1","description":"d","mcp_exposed":false,
  "created_at":"...","updated_at":"...",
  "nodes":[ /* §4 Node[] */ ], "edges":[ /* §4 Edge[] */ ]
}
```
- **PUT 동작:** 기존 nodes+edges를 한 트랜잭션에서 전부 삭제 후 재삽입. `node.id`→`nodes.node_key`, `edge.id`→`edges.edge_key` 문자열 보존. 실패 시 rollback(기존 그래프 유지).
- **Node 와이어 shape**(저장/응답 동일): `{"id":str,"type":"api_call|start|end|transform","label":str,"operation_id":int|null,"params":{"path":{},"query":{},"header":{},"body":null},"position":{"x":num,"y":num}}`.
- **Edge 와이어 shape:** `{"id":str,"source":str,"target":str,"data_mapping":[{"from":"$.id","to":"params.path.petId"}]}`.
  - ⚠️ `data_mapping` 항목의 와이어 키는 **반드시 `"from"`** (Pydantic alias `from_`는 내부용). 라운드트립 보존 검증 완료.

### executions

| Method | Path | 응답(200) |
|--------|------|-----------|
| GET | `/api/executions/{exec_id}` | `ExecutionResult` (미존재 404) |

`ExecutionResult` (run 응답 & 조회 동일 shape, §5.0):
```json
{
  "execution_id":1,"workflow_id":1,"status":"running|success|failed",
  "started_at":"...","finished_at":"...|null","result": <any>|null,
  "logs":[{"node_key":str,"seq":int,"status":"success|failed|skipped",
           "input":{}|null,"output":<any>|null,"error":str|null,"timestamp":"..."}]
}
```

### 상태코드 규약 (구현 확인)
- 생성 포함 성공 **200 통일**.
- 검증 실패 422(FastAPI 기본).
- 미존재 404 `{"detail":...}` (= `ErrorResponse`).
- **노드 실패는 HTTP 200 + `status:"failed"`** (엔진은 죽지 않음, 로그 보존). 스모크로 확인.
- 업로드 검증 실패(확장자/크기/인코딩/빈 파일/SSRF/URL fetch 실패): **400**.
- engine 미가용(`EngineUnavailableError`): **503**. 엔진 내부 예외(노드 실패 아님): **500** + execution `failed` 마킹.

---

## 3. engine에 기대한 import 경로 / 시그니처 (§6) — **LIVE 결합 확인됨**

백엔드는 `backend/engine_bridge.py`를 통해서만 호출(직접 `engine` import은 bridge 1곳).
```python
from engine import run_workflow, parse_openapi, ParseResult   # 3개 export 모두 존재 확인
```
- `parse_openapi(raw_content: str, source_hint: str|None) -> ParseResult`
  - 사용 필드: `spec_version`, `base_url`, `operations:list[dict]`, `warnings:list[str]`.
  - `operations[]` dict 키(백엔드가 그대로 INSERT): `operation_id, method, path, base_url?, summary?, params_schema, request_schema?, response_schema?, auth?`.
  - 실제 Petstore형 스펙 파싱 → operation_count≥1, params_schema 평탄구조 정상 확인.
- `async run_workflow(graph: WorkflowGraph, initial_input=None, auth=None, on_node_event=None) -> ExecutionResult`
  - 반환은 `ExecutionResult`/`model_dump` 보유 객체/dict 모두 수용해 bridge가 정규화.
  - 백엔드가 반환 객체의 `execution_id`/`workflow_id`를 DB 값으로 덮어쓴 뒤 영속화.
  - 노드 실패 시 엔진이 예외 던지지 말고 `status:"failed"` + logs 반환해야 함(확인됨).

> engine이 만약 export를 누락하면 bridge가 503으로 graceful 처리(앱은 계속 기동). 현재는 정상.

---

## 4. frontend가 알아야 할 사항

- **노드 후보 목록**: `GET /api/specs/{spec_id}/operations` → 각 `OperationOut.id`를 `Node.operation_id`(int)로 사용.
- **파라미터 폼**: `OperationOut.params_schema`는 §3 평탄 구조 `{path:[],query:[],header:[]}`, 각 항목 `{name,type,required,enum?,description?,default?}`.
- **그래프 저장**: `PUT /api/workflows/{id}` body = `{nodes:[Node], edges:[Edge]}` (§4 키 정확히). 응답은 저장된 `WorkflowDetail`(서버가 정규화한 그래프를 다시 받음 — 캔버스 동기화에 사용 가능).
- **`data_mapping` 키는 `"from"`/`"to"`** — 자유 텍스트 템플릿 금지(계약 §7). 엣지 단위 매핑만.
- **실행**: `POST /api/workflows/{id}/run` → 동기 완료된 `ExecutionResult` 즉시 반환(폴링 불필요). 추가로 `GET /api/executions/{id}`로 재조회 가능.
- **노드 실패도 HTTP 200**: `body.status==="failed"`와 `logs[].status`로 UI 분기(에러 throw 아님).
- **CORS 전체 허용** — 동일 출처/별도 포트 프론트 모두 호출 가능.
- 루트 `/`는 `templates/index.html`을 렌더하므로 frontend가 그 파일을 채우면 됨. 정적은 `/static/*`.

---

## 5. 미구현 / 의도적 제외 (1차 범위 외)

- `GET /api/executions/{exec_id}/stream` (SSE) — **미구현**. 동기 run + 폴링으로 대체(Assumption 2). 추후 `run_workflow`의 `on_node_event` 콜백을 SSE로 중계 예정(engine_bridge가 콜백 인자 이미 통과시킴).
- `POST /api/workflows/{id}/expose` (MCP 노출 토글) — **미구현**(우선순위 낮음, §5.1). repository에 `workflows.set_mcp_exposed(...)` 헬퍼는 준비됨(엔드포인트만 추가하면 됨). MCP 실제 노출은 `backend/mcp_server.py`(mcp-engineer 소유).
- 인증/세션 없음(Assumption 1). 시크릿 DB 영구저장 안 함 — 실행 시 `RunRequest.auth`로 주입(Assumption 8).

---

## 6. 검증 결과

`python -c "from backend.app import app"` import 스모크 OK.
TestClient(lifespan 포함) 통합 스모크 **전부 통과**:
- health, 워크플로우 생성/목록/조회/PUT(그래프 교체)/삭제
- `data_mapping` `"from"` 와이어 키 라운드트립 보존
- node.params.path 정적값 보존
- 실제 OpenAPI 3.0 스펙 업로드 → operations 저장(method/path/params_schema 검증)
- `/run` → HTTP 200 + `status:"failed"`(노드 실패 의미론) + logs 2건 + execution 영속화/재조회
- 404(workflow/execution/spec) · 400(잘못된 확장자) 경로

> 실행 엔진(`engine.run_workflow`)·파서(`engine.parse_openapi`)가 이미 구현되어 있어 **end-to-end 런타임 결합이 실제로 동작**함을 확인(스텁 아님).

---

## 수정 이력 (QA BUG-1 / BUG-2 정밀 수정)

> 트리거: `_workspace/05_qa_report.md` 의 critical 2건 (둘 다 backend 소유). 계약 §6/§5/§4 키·시그니처 유지. Python 3.11 호환.

### 🔴 BUG-1 — `operation_resolver` 미주입 (경계3: backend ↔ engine)

**근본 원인:** engine `run_workflow`는 keyword-only `operation_resolver: Callable[[int], dict|None]`를 받고 None이면 모든 api_call 노드를 `"no operation_resolver provided to engine"`로 즉시 실패시킨다. backend 래퍼·호출부가 이 인자를 전혀 다루지 않았다.

**수정 1 — `backend/engine_bridge.py` (`run_workflow` 래퍼):**
- 파라미터 `operation_resolver: Optional[Callable[[int], Optional[dict]]] = None` 추가.
- 엔진 호출에 `operation_resolver=operation_resolver`로 그대로 전달(엔진의 keyword-only 파라미터에 매칭).
- docstring에 "api_call 노드 실행에 필수" 명시.

**수정 2 — `backend/routers/workflows.py` (`/run` 핸들러):**
- `from ..repositories import specs as specs_repo` import 추가.
- `/run` 핸들러 내부(execution 생성 직후, await 직전)에 클로저 콜백 정의:
  ```python
  def _operation_resolver(op_id: int):
      op = specs_repo.get_operation(conn, op_id)
      return op.model_dump() if op is not None else None
  ```
  - **resolver 구성 방식:** `specs_repo.get_operation(conn, operation_pk) -> OperationOut|None` 을 호출, 결과를 `.model_dump()`로 **dict** 변환(엔진은 `op.get(...)` 사용). 반환 dict 키(`method/path/base_url/auth/request_schema/params_schema/...`)는 `OperationOut`와 100% 호환(QA §1.4 확인).
  - 조회 결과 없으면 `None` 반환.
  - **커넥션 수명:** 콜백이 `await engine_bridge.run_workflow(...)` 실행 중 동기 호출되므로, FastAPI `Depends(get_db)`로 주입된 `conn`이 살아있는 핸들러 스코프 안에서 클로저로 정의 → 커넥션 수명 보장.
- `engine_bridge.run_workflow(...)` 호출에 `operation_resolver=_operation_resolver` 추가.

### 🔴 BUG-2 — `GET /editor/{id}` 페이지 라우트 부재 (경계1: backend ↔ frontend)

**근본 원인:** `templates/index.html`이 `/editor/${wf.id}`로 이동하고 `editor.html`이 `{{ workflow_id }}`(meta `workflow-id`)를 기대하나 `backend/app.py`엔 `GET /`만 있어 404.

**수정 — `backend/app.py`:**
- `GET /editor/{workflow_id}` 라우트 추가. `Jinja2Templates`로 `editor.html` 렌더, 컨텍스트에 변수명 정확히 **`workflow_id`** 주입.
- `editor.html` 부재 시 JSON 폴백(`{"app":"mcp-provider","editor":<id>}`) — index의 기존 패턴과 동일. (단순 처리: 미존재 workflow_id여도 템플릿만 렌더 — 에디터가 빈 그래프로 동작, 404 불필요.)

### 재검증 결과 (TestClient e2e, lifespan 포함)

- `python -c "from backend.app import app"` → **import OK**, `/editor/{workflow_id}` 라우트 등록 확인.
- e2e: petstore 3.0 최소 스펙(GET /get + POST /post, server=httpbin.org) 업로드(200, operation_count=2) → 워크플로우 생성(id=1) → start→GET→POST→end + data_mapping PUT 저장(200) → `/run`(200).
- 실행 로그:
  ```
  seq=0 node_0 start    success
  seq=1 node_1 api_call failed   error='HTTP 503'   ← resolver 호출됨, op 조회 후 실제 HTTP 단계 도달
  seq=2 node_2 api_call skipped  (upstream failure)
  seq=3 node_3 end      skipped
  ```
- **resolver 호출 확인:** 노드 에러가 더 이상 `"no operation_resolver provided"`가 **아님**. `HTTP 503`은 샌드박스 아웃바운드 차단(직접 `httpx.get('https://httpbin.org/get')`도 503 반환 확인) — 즉 resolver가 op(method/path/base_url)를 정상 반환 → 엔진이 요청 빌드 → HTTP 호출까지 진행한 증거. **BUG-1 해소 확정.**
- **`/editor/1` → 200**, 응답 HTML에 `workflow-id` meta + `workflow_id` 값 주입 확인. **BUG-2 해소 확정.**

### 변경 파일 요약

| 파일 | 변경 |
|------|------|
| `backend/engine_bridge.py` | `run_workflow` 래퍼에 `operation_resolver` 파라미터 추가·엔진 전달 |
| `backend/routers/workflows.py` | `specs` repo import + `/run` 핸들러에 `_operation_resolver` 클로저 구성·주입 |
| `backend/app.py` | `GET /editor/{workflow_id}` 라우트 추가(컨텍스트 `workflow_id`) |

> 소유권 준수: `engine/`·`templates/`·`static/` 미수정(읽기만). 계약 키·시그니처 유지. requirements.txt 변경 없음(신규 의존성 불필요).

---

## 기능추가 이력

### ➕ FEAT-1 — operation 단건 조회 엔드포인트 (`GET /api/operations/{operation_id}`)

**목적/소비자:** **프론트가 노드의 `operation_id`(int PK)만으로 해당 operation 메타(응답 스키마·입력 파라미터 스키마)를 조회**한다. 즉, 캔버스에서 노드 클릭 시 `Node.operation_id` 하나로 `params_schema`(폼 렌더용 §3 평탄 구조)와 `response_schema`/`request_schema`/`auth`를 가져오는 경로. 기존 `GET /api/specs/{spec_id}/operations`(스펙 전체 목록)와 달리 spec_id 없이 op 단건만 필요할 때 사용.

**계약 (addendum to §5.2):**
- `GET /api/operations/{operation_id}` → **200 `OperationOut`** (기존 §5.2 모델 그대로) / **404 `ErrorResponse`**(`{"detail": "..."}`).
- 응답 shape = `OperationOut`: `{id, spec_id, operation_id(str), method, path, base_url?, summary?, params_schema, request_schema?, response_schema?, auth?}`. **JSON 컬럼은 파싱된 객체로 노출**(기존 패턴 동일).

**구현:**
- 신규 라우터 `backend/routers/operations.py` (`prefix="/api/operations"`). 기존 specs 라우터는 `/api/specs` prefix라 경로 충돌·prefix 오염 없이 **정확히 `/api/operations/{operation_id}`** 보장.
- `backend/app.py`에 `operations_router` import + `include_router` 등록.
- 핸들러는 기존 `specs_repo.get_operation(conn, operation_pk) -> OperationOut|None` **재사용**(신규 SELECT 작성 불필요). None이면 404.

**검증 결과 (TestClient e2e, lifespan 포함):**
- `python -c "from backend.app import app"` import OK + 라우트 목록에 `/api/operations/{operation_id}` 존재 확인.
- OpenAPI 3.0 스펙 업로드 → `operations[0].id`로 `GET /api/operations/{id}` → **200**, 응답에 `params_schema`(`{path:[{name:petId,type:integer,required:true}],query:[],header:[]}`)·`response_schema`(`{"200":{...}}`) 포함 확인.
- 없는 id(`999999`) → **404** `{"detail":"Operation 999999 not found."}` 확인.

| 파일 | 변경 |
|------|------|
| `backend/routers/operations.py` | 신규. `/api/operations/{operation_id}` 단건 조회 라우터 |
| `backend/app.py` | `operations_router` import + `include_router` 등록 |

> 소유권 준수: `backend/`만 수정. `engine/`·`templates/`·`static/` 미수정. requirements.txt 변경 없음.
