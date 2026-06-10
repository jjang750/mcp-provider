# 01 — Architect Contracts (mcp-provider)

> **단일 진실 공급원 (Single Source of Truth).**
> backend-engineer / mcp-engineer / frontend-engineer / qa-integrator는 이 문서의 키 이름·타입·시그니처를 **그대로** 사용한다. 임의 변경 금지. 변경이 필요하면 architect에 요청한다.
>
> 대상 런타임: **Python 3.11**. 단일 사용자 / 소규모. SQLite 단일 파일. 단일 FastAPI 앱 + 별도 MCP 서버 진입점.
>
> 1차 MVP 검증 시나리오: *Petstore swagger.json 업로드 → operation 추출 → GET/POST 노드 2개 배치 → 엣지 연결 → 순차 실행 → 실행 로그 표시*.

---

## 0. Assumptions (가정)

이 설계는 다음 가정 위에 선다. 가정이 틀리면 해당 섹션을 재검토한다.

1. **단일 사용자, 인증 없음.** UI/REST에 로그인·세션 개념 없음. 멀티테넌시 컬럼(`owner_id` 등) 없음.
2. **동기 실행이 기본.** `POST /api/workflows/{id}/run`은 실행을 끝까지 동기로 돌리고 최종 결과를 반환한다. 진행상황 실시간 표시는 SSE(`GET /api/executions/{id}/stream`)로 **선택 제공**(1차 구현 우선순위 낮음, 폴링으로 대체 가능).
3. **순차 실행만.** 그래프는 위상정렬 후 한 줄로 직렬 실행. 병렬 분기 실행은 v1 범위 외 (분기 엣지가 있어도 위상순으로 순차 처리).
4. **노드 출력은 JSON.** `api_call` 노드의 출력은 HTTP 응답 본문을 JSON으로 파싱한 값. JSON이 아니면 `{"raw": "<text>", "status_code": n}`로 래핑.
5. **데이터 매핑은 edge `data_mapping` 단일 방식** (§7 결정 참조). 노드 `params` 값에 `{{...}}` 템플릿 문자열을 쓰는 방식은 **계약상 금지**.
6. **DB의 모든 JSON 컬럼은 `TEXT`에 `json.dumps` 직렬화.** SQLite의 JSON1 함수에 의존하지 않는다(이식성·단순성). 리포지토리 계층이 직렬화/역직렬화 책임.
7. **ID 타입 이원화.**
   - DB 행 PK는 **정수**(`INTEGER PRIMARY KEY`).
   - 그래프 JSON 안의 노드/엣지 ID(`node.id`, `edge.id`)는 **문자열**(예: `"node_1"`, `"edge_1"`). 이는 Drawflow 캔버스 로컬 식별자이며 DB PK와 별개다. (§4, §6 참조)
8. **인증 정보(토큰 등)는 실행 시점에 주입.** 1차에서는 spec 단위 `auth` 메타만 저장하고, 실제 토큰 값은 `POST /run`의 `auth` 필드 또는 워크플로우 노드 params에서 받는다. DB에 시크릿 영구 저장하지 않는다.
9. **base_url은 operation에 귀속.** 파서가 스펙에서 base_url을 결정해 `operations.base_url`에 저장. 노드는 operation_id를 통해 base_url을 상속.
10. **타임스탬프는 ISO-8601 UTC 문자열**(`datetime.now(timezone.utc).isoformat()`). SQLite는 `TEXT`로 저장.

---

## 1. 시스템 구조 & 모듈 경계

```
[브라우저] ──htmx/Drawflow──▶ FastAPI(app.py) ──▶ SQLite (단일 파일: mcp_provider.db)
                                   │ import (순수 모듈)
                                   ▼
                            engine/ (파서 + 실행엔진)  ──HTTP──▶ 외부 실제 API
[외부 MCP 클라이언트] ◀── mcp_server.py (mcp_exposed 워크플로우 → MCP 도구) ── engine/
```

- **backend-engineer**: FastAPI 앱·라우터·DB·리포지토리·Pydantic 모델. engine 모듈을 **import만** 한다.
- **mcp-engineer**: `engine/` 패키지(파서, 실행엔진) — FastAPI 비의존 **순수 모듈** — + `mcp_server.py`.
- **frontend-engineer**: `templates/`, `static/`. REST 계약만 소비.
- **qa-integrator**: 경계면(REST shape ↔ 캔버스 직렬화 ↔ 엔진 그래프) 정합성 검증.

> 핵심 경계 규칙: **그래프 JSON shape(§4)** 과 **REST 응답 shape(§5)** 은 세 모듈이 공유하는 가장 깨지기 쉬운 계약이다. 이 문서가 키 이름까지 고정한다.

---

## 2. 확정 SQLite 스키마 (DDL)

파일: `mcp_provider.db`. 앱 시작 시 아래 DDL을 **idempotent** 실행(`CREATE TABLE IF NOT EXISTS`). `db.py`가 소유.

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS specs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    source_type   TEXT    NOT NULL CHECK (source_type IN ('file', 'url')),
    source_ref    TEXT,                          -- 원본 파일명 또는 URL
    spec_version  TEXT,                          -- '2.0' | '3.0.x' | '3.1.x' (감지값)
    raw_content   TEXT    NOT NULL,              -- 업로드 원본(JSON/YAML 텍스트)
    parsed_at     TEXT,                          -- ISO-8601 UTC, 파싱 성공 시각
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_id         INTEGER NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
    operation_id    TEXT    NOT NULL,            -- 스펙의 operationId(없으면 파서 생성)
    method          TEXT    NOT NULL,            -- 대문자: GET/POST/PUT/DELETE/PATCH...
    path            TEXT    NOT NULL,            -- '/pet/{petId}'
    base_url        TEXT,                        -- 파서가 결정한 서버 base URL
    summary         TEXT,
    params_schema   TEXT    NOT NULL DEFAULT '{}',  -- JSON: {path:[],query:[],header:[]} (§3)
    request_schema  TEXT,                        -- JSON: requestBody JSON Schema(없으면 NULL)
    response_schema TEXT,                        -- JSON: {"200": <schema>, ...}(없으면 NULL)
    auth            TEXT,                        -- JSON: {"type":"bearer"|"apiKey"|"none", ...}(§3)
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operations_spec ON operations(spec_id);

CREATE TABLE IF NOT EXISTS workflows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    description  TEXT,
    mcp_exposed  INTEGER NOT NULL DEFAULT 0,     -- 0/1 (bool)
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- DB PK (정수)
    workflow_id   INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    node_key      TEXT    NOT NULL,              -- 그래프 JSON의 node.id (문자열, 예 'node_1')
    operation_id  INTEGER REFERENCES operations(id) ON DELETE SET NULL,  -- api_call일 때만
    type          TEXT    NOT NULL,              -- 'api_call'|'start'|'end'|'transform'
    label         TEXT    NOT NULL DEFAULT '',
    params        TEXT    NOT NULL DEFAULT '{}', -- JSON (§4 Node.params)
    position_x    REAL    NOT NULL DEFAULT 0,
    position_y    REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_nodes_workflow ON nodes(workflow_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_nodes_wf_key ON nodes(workflow_id, node_key);

CREATE TABLE IF NOT EXISTS edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,  -- DB PK (정수)
    workflow_id     INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    edge_key        TEXT    NOT NULL,            -- 그래프 JSON의 edge.id (문자열)
    source_node_key TEXT    NOT NULL,            -- node.node_key 참조(문자열)
    target_node_key TEXT    NOT NULL,
    data_mapping    TEXT    NOT NULL DEFAULT '[]'  -- JSON 배열 (§4 Edge.data_mapping)
);
CREATE INDEX IF NOT EXISTS idx_edges_workflow ON edges(workflow_id);

CREATE TABLE IF NOT EXISTS executions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id  INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    status       TEXT    NOT NULL,              -- 'running'|'success'|'failed'
    started_at   TEXT    NOT NULL,
    finished_at  TEXT,
    result       TEXT                           -- JSON: 최종 출력(§5 ExecutionResult.result)
);
CREATE INDEX IF NOT EXISTS idx_executions_workflow ON executions(workflow_id);

CREATE TABLE IF NOT EXISTS execution_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id  INTEGER NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    node_key      TEXT    NOT NULL,             -- 그래프 노드 식별자(문자열)
    seq           INTEGER NOT NULL,             -- 실행 순서(0부터)
    status        TEXT    NOT NULL,             -- 'success'|'failed'|'skipped'
    input         TEXT,                         -- JSON: 노드에 주입된 최종 input
    output        TEXT,                         -- JSON: 노드 출력
    error         TEXT,                         -- 실패 시 메시지(성공 시 NULL)
    timestamp     TEXT    NOT NULL              -- ISO-8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_logs_execution ON execution_logs(execution_id);
```

**결정 이유:**
- `node_key`/`edge_key`를 별도 컬럼으로 둔 이유: 캔버스(Drawflow)는 자체 문자열 ID를 쓰고, 워크플로우 저장 시 "전부 삭제 후 재삽입"(§5 PUT) 하면 정수 PK가 매번 바뀐다. `edges`가 정수 PK를 참조하면 깨지므로, **그래프 내부 참조는 문자열 key로 통일**한다.
- JSON을 `TEXT`로 저장: JSON1 확장 의존 제거, 리포지토리에서 직렬화 일원화(Assumption 6).
- `bool`은 SQLite에 `INTEGER 0/1`. Pydantic에서 `bool`로 노출.

---

## 3. operations 내부 JSON shape (파서 출력 ↔ 프론트 폼 입력)

mcp-engineer 파서가 채우고, frontend 파라미터 폼이 읽는다. 키 고정.

```jsonc
// operations.params_schema (평탄 구조 — 프론트가 폼으로 직접 렌더)
{
  "path":   [{"name": "petId", "type": "integer", "required": true,  "description": ""}],
  "query":  [{"name": "status","type": "string",  "required": false, "enum": ["available","pending","sold"], "description": ""}],
  "header": [{"name": "X-Key", "type": "string",  "required": false, "description": ""}]
}
// 각 항목 필수 키: name, type, required.  선택 키: enum, description, default.

// operations.request_schema  — requestBody 있을 때만(없으면 NULL)
{
  "content_type": "application/json",
  "schema": { /* 해석된 JSON Schema (ref 펼침) */ },
  "required": true
}

// operations.response_schema — 없으면 NULL
{ "200": { /* JSON Schema */ }, "default": { /* ... */ } }

// operations.auth
{ "type": "bearer" }                                  // http bearer
{ "type": "apiKey", "in": "header", "name": "api_key" } // apiKey
{ "type": "none" }                                     // 인증 없음
```

`type` 값은 JSON Schema 기본형(`string`|`integer`|`number`|`boolean`|`array`|`object`)을 사용.

---

## 4. 확정 노드 / 엣지 / 워크플로우 그래프 JSON shape

**이것이 캔버스↔백엔드↔엔진 공유 경계.** 키 이름·중첩 구조 고정. 추측 금지.

```jsonc
// ===== Node =====
{
  "id": "node_1",                 // string, 그래프 내 유일(= DB nodes.node_key)
  "type": "api_call",             // "api_call" | "start" | "end" | "transform"
  "label": "GET /pet/{petId}",    // string, 표시용
  "operation_id": 42,             // integer | null. api_call일 때 operations.id(DB PK), 그 외 null
  "params": {                     // 사용자가 채운 정적 기본값. 동적 주입은 edge.data_mapping이 덮어씀
    "path":   {"petId": 10},
    "query":  {},
    "header": {},
    "body":   null                // object | null (request body 정적 기본값)
  },
  "position": {"x": 120, "y": 80} // number
}

// ===== Edge =====
{
  "id": "edge_1",                 // string, 유일(= DB edges.edge_key)
  "source": "node_0",             // 선행 노드 id(문자열)
  "target": "node_1",             // 후행 노드 id(문자열)
  "data_mapping": [               // 선행 노드 출력 → 후행 노드 input 주입 규칙(0개 이상)
    {"from": "$.id", "to": "params.path.petId"}
  ]
}

// ===== WorkflowGraph (저장/로드/실행 단위) =====
{
  "workflow_id": 7,               // integer
  "nodes": [ /* Node[] */ ],
  "edges": [ /* Edge[] */ ]
}
```

### data_mapping 항목 의미 (엔진 계약)
- `from`: **선행 노드(source)의 출력 객체**에 대한 경로. 형식 = JSONPath의 단순 부분집합.
  - 루트 `$` = 선행 노드 output 전체.
  - dotted/인덱스 접근만 지원: `$.id`, `$.data.items[0].name`. (필터·와일드카드 미지원 — `jsonpath-ng` 사용해도 무방하나 위 부분집합만 보장.)
- `to`: **후행 노드(target)의 input 경로**. 항상 `params.`로 시작하는 dotted 경로.
  - 허용 prefix: `params.path.<k>`, `params.query.<k>`, `params.header.<k>`, `params.body` 또는 `params.body.<k>...`.
- 적용 순서: 노드 정적 `params` 로드 → 들어오는 모든 edge의 `data_mapping` 순차 적용(덮어쓰기) → 최종 input 확정.
- 매핑 실패(`from` 경로 없음): 해당 매핑 스킵 + `execution_logs.error`에 경고 누적(노드 자체는 계속).

**결정 이유:** §7 참조.

---

## 5. REST API 계약

베이스 prefix `/api`. 모든 모델은 Pydantic v2. JSON 컬럼은 응답에서 파싱된 객체로 노출(문자열 아님).

### 5.0 공통 모델

```python
from pydantic import BaseModel, Field
from typing import Any, Literal, Optional

NodeType = Literal["api_call", "start", "end", "transform"]

class Position(BaseModel):
    x: float = 0
    y: float = 0

class NodeParams(BaseModel):
    path:   dict[str, Any] = Field(default_factory=dict)
    query:  dict[str, Any] = Field(default_factory=dict)
    header: dict[str, Any] = Field(default_factory=dict)
    body:   Optional[Any]  = None

class Node(BaseModel):
    id: str
    type: NodeType
    label: str = ""
    operation_id: Optional[int] = None
    params: NodeParams = Field(default_factory=NodeParams)
    position: Position = Field(default_factory=Position)

class DataMappingItem(BaseModel):
    from_: str = Field(alias="from")   # 직렬화/역직렬화 시 키는 "from"
    to: str
    model_config = {"populate_by_name": True}

class Edge(BaseModel):
    id: str
    source: str
    target: str
    data_mapping: list[DataMappingItem] = Field(default_factory=list)

class WorkflowGraph(BaseModel):
    workflow_id: int
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)

class ErrorResponse(BaseModel):
    detail: str
```
> `DataMappingItem.from_`은 Python 예약어 회피용 alias. **와이어 포맷의 키는 반드시 `"from"`** (frontend/engine 모두 `"from"` 사용).

### 5.1 엔드포인트 목록

| Method | Path | 설명 | 1차 우선 |
|--------|------|------|:---:|
| POST | `/api/specs/upload` | 파일 업로드 → 파싱 → operations 저장 | ✅ |
| POST | `/api/specs/from-url` | URL fetch → 파싱 → 저장 | ✅ |
| GET | `/api/specs` | 스펙 목록 | ✅ |
| GET | `/api/specs/{spec_id}/operations` | 스펙의 operation(노드 후보) 목록 | ✅ |
| GET | `/api/workflows` | 워크플로우 목록 | ✅ |
| POST | `/api/workflows` | 워크플로우 생성(빈 그래프) | ✅ |
| GET | `/api/workflows/{id}` | 단건 + 그래프 조회 | ✅ |
| PUT | `/api/workflows/{id}` | 그래프 저장(nodes+edges 교체) | ✅ |
| DELETE | `/api/workflows/{id}` | 삭제 | ✅ |
| POST | `/api/workflows/{id}/run` | 순차 실행(동기) → 결과 반환 | ✅ |
| GET | `/api/executions/{exec_id}` | 실행 상태 + 로그 | ✅ |
| GET | `/api/executions/{exec_id}/stream` | SSE 진행 스트림 | ⬜ (선택) |
| POST | `/api/workflows/{id}/expose` | MCP 노출 토글 | ⬜ (낮음) |

### 5.2 요청/응답 모델 (필드 확정)

```python
# ---- specs ----
class SpecSummary(BaseModel):
    id: int
    name: str
    source_type: Literal["file", "url"]
    spec_version: Optional[str]
    created_at: str

class OperationOut(BaseModel):
    id: int                       # operations.id (= Node.operation_id로 사용)
    spec_id: int
    operation_id: str             # 스펙상의 operationId
    method: str
    path: str
    base_url: Optional[str]
    summary: Optional[str]
    params_schema: dict[str, Any] # §3 평탄 구조
    request_schema: Optional[dict[str, Any]]
    response_schema: Optional[dict[str, Any]]
    auth: Optional[dict[str, Any]]

class SpecUploadResult(BaseModel):
    spec: SpecSummary
    operation_count: int
    operations: list[OperationOut]
    warnings: list[str] = []      # 파서 부분실패 경고

# POST /api/specs/upload  -> multipart/form-data, field name = "file" (UploadFile)
#   응답 200: SpecUploadResult
# POST /api/specs/from-url
class SpecFromUrlRequest(BaseModel):
    url: str
    name: Optional[str] = None
#   응답 200: SpecUploadResult
# GET /api/specs                       -> list[SpecSummary]
# GET /api/specs/{spec_id}/operations  -> list[OperationOut]

# ---- workflows ----
class WorkflowSummary(BaseModel):
    id: int
    name: str
    description: Optional[str]
    mcp_exposed: bool
    created_at: str
    updated_at: str

class WorkflowCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None

class WorkflowDetail(BaseModel):
    id: int
    name: str
    description: Optional[str]
    mcp_exposed: bool
    created_at: str
    updated_at: str
    nodes: list[Node]             # §4
    edges: list[Edge]             # §4

class WorkflowSaveRequest(BaseModel):   # PUT body (그래프 교체)
    nodes: list[Node]
    edges: list[Edge]
    # name/description 변경도 허용(선택)
    name: Optional[str] = None
    description: Optional[str] = None

# GET  /api/workflows            -> list[WorkflowSummary]
# POST /api/workflows            (WorkflowCreateRequest) -> WorkflowDetail (빈 그래프)
# GET  /api/workflows/{id}       -> WorkflowDetail
# PUT  /api/workflows/{id}       (WorkflowSaveRequest)   -> WorkflowDetail
# DELETE /api/workflows/{id}     -> {"deleted": true} (200) / 404 ErrorResponse

# ---- run / executions ----
class RunRequest(BaseModel):
    initial_input: dict[str, Any] = Field(default_factory=dict)  # start 노드 외부 입력
    auth: dict[str, Any] = Field(default_factory=dict)           # {"token": "...", "api_key": "..."} 실행시 주입

class NodeLog(BaseModel):
    node_key: str
    seq: int
    status: Literal["success", "failed", "skipped"]
    input: Optional[dict[str, Any]]
    output: Optional[Any]
    error: Optional[str]
    timestamp: str

class ExecutionResult(BaseModel):       # run 응답 & 엔진 반환(§6과 동일 shape)
    execution_id: int
    workflow_id: int
    status: Literal["running", "success", "failed"]
    started_at: str
    finished_at: Optional[str]
    result: Optional[Any]               # 마지막(end 또는 종단) 노드 출력
    logs: list[NodeLog]

# POST /api/workflows/{id}/run   (RunRequest) -> ExecutionResult (동기, 완료 후 반환)
# GET  /api/executions/{exec_id}              -> ExecutionResult
```

**상태 코드 규약:** 성공 200(생성도 200으로 통일 — 단순화), 검증 실패 422(FastAPI 기본), 미존재 404(`ErrorResponse`), 실행 중 노드 오류는 **HTTP 200 + `status:"failed"`**(엔진은 죽지 않음, §6).

**결정 이유:** 생성에 201 대신 200 통일은 htmx/프론트 분기 단순화. 노드 실패를 200으로 두는 이유 = 부분 결과·로그를 정상 페이로드로 받아 UI에 표시해야 하기 때문(에러를 HTTP 오류로 던지면 로그가 유실됨).

---

## 6. 엔진 진입 인터페이스 (mcp-engineer 소유, backend·MCP 공유)

`engine/` 패키지의 순수 모듈. FastAPI 비의존.

```python
# engine/executor.py
async def run_workflow(
    graph: WorkflowGraph,                      # §4/§5.0 모델 (또는 동일 키의 dict)
    initial_input: dict | None = None,         # start 노드 외부 입력
    auth: dict | None = None,                  # {"token":..., "api_key":...} 실행시 주입
    on_node_event=None,                        # Optional[Callable[[NodeLog], None|Awaitable]] SSE용
) -> ExecutionResult: ...                      # §5.0 ExecutionResult 와 동일 shape
```

```python
# engine/parser.py
class ParseResult(BaseModel):
    spec_version: Optional[str]
    base_url: Optional[str]
    operations: list[dict]     # OperationOut(§5.2)의 DB-비의존 부분(id/spec_id 제외) 키와 일치
    warnings: list[str]

def parse_openapi(raw_content: str, source_hint: str | None = None) -> ParseResult: ...
#   raw_content: 업로드 원본(JSON/YAML 텍스트). source_hint: 파일명/URL(YAML 판별 보조).
#   부분 실패 허용: 파싱 가능한 operation만 채우고 warnings에 누적.
```

- backend는 `parse_openapi`를 호출해 받은 `operations`를 `operations` 테이블에 INSERT(id/spec_id 부여).
- backend는 `POST /run`에서 그래프를 로드(`WorkflowGraph`)해 `run_workflow` 호출, 반환 `ExecutionResult`를 `executions`/`execution_logs`에 저장 후 그대로 응답.
- **노드 출력 참조 기준점**: `data_mapping[].from`의 `$`는 **source 노드의 `output`**(HTTP 응답 본문 JSON). 엔진은 노드별 `output`을 메모리 맵 `{node_key: output}`으로 보관.
- 실행 절차: 그래프 검증(순환→거부) → 위상정렬 → 노드별(정적 params + 들어오는 edge data_mapping 주입) HTTP 호출 → 로그 기록. 실패 시 그 지점 중단, 이후 노드는 `skipped`.

`engine/__init__.py`는 `run_workflow`, `parse_openapi`, `ParseResult`를 export 한다(backend import 경로 안정화).

---

## 7. 데이터 매핑 방식 — **택일: edge `data_mapping` 배열 (확정)**

**결정: 노드 간 데이터 전달은 오로지 edge의 `data_mapping` 배열로 한다.** 노드 `params` 값 안에 `{{node_x.output.field}}` 템플릿 문자열을 넣는 방식은 **계약상 채택하지 않는다.**

**이유:**
1. **단일 책임·단일 파서.** 템플릿 방식은 엔진이 모든 params 값을 문자열 스캔해 `{{}}`를 해석해야 하고, 정수/객체 값에 템플릿이 섞이면 타입이 깨진다. data_mapping은 "출력 경로 → 입력 경로" 구조화 데이터라 파싱이 단순하고 타입 보존이 쉽다.
2. **그래프 위상과 일치.** 데이터 의존이 곧 엣지다. 매핑이 엣지에 붙으면 위상정렬 의존성과 데이터 의존성이 한 곳에서 표현돼 검증(순환·도달성)이 자연스럽다. 템플릿은 엣지 없이도 숨은 의존을 만들어 정렬을 오염시킨다.
3. **UI 친화.** Drawflow에서 엣지를 그리는 행위와 매핑 지정 UI가 1:1로 대응(엣지 클릭 → 매핑 편집). 프론트가 자유 텍스트 템플릿 파서를 만들 필요가 없다.
4. **계약 명확성.** `{from, to}`는 키가 고정돼 세 모듈이 추측 없이 구현 가능. 스킬 권장사항(권장: edge data_mapping 단일 방식)과도 일치.

> 결과적으로 노드 `params`는 **정적 기본값만** 담는다. 동적 값은 전부 들어오는 엣지의 `data_mapping`이 채운다. start 노드 외부 입력은 `run_workflow(initial_input=...)`로 들어와, start 노드의 `output`으로 노출되어 후속 엣지 매핑의 `$` 기준점이 된다.

---

## 8. MCP 도구 매핑 규칙 (설계 확정 / 1차 구현 우선순위 낮음)

- 노출 단위: `mcp_exposed=1`인 **워크플로우 1개 = MCP 도구 1개**.
- **도구명:** `workflow_{id}_{slug}` — `slug` = `name`을 소문자화, 영숫자 외 문자를 `_`로 치환, 연속 `_` 축약, 양끝 `_` 제거. 예: 워크플로우 id=7, name="Find Pet" → `workflow_7_find_pet`.
- **inputSchema (JSON Schema, object):** 워크플로우의 `start` 노드가 요구하는 외부 입력 = **어떤 edge의 `data_mapping` `from`으로도 채워지지 않는, 그리고 정적 params에도 없는** 필수 파라미터들의 합집합.
  - 각 필요한 입력은 해당 operation의 `params_schema`에서 `name/type/required`를 가져와 `properties`/`required`로 변환.
  - start 노드가 없으면 = 위상 첫 노드의 미충족 필수 파라미터를 입력으로 노출.
- **핸들러:** 도구 호출 시 인자 `tool_args` → `run_workflow(graph, initial_input=tool_args, auth=<서버 설정>)` 실행 → `ExecutionResult.result`(종단 노드 출력)를 도구 결과로 반환. 실패 시 `status:"failed"` + 마지막 `error`를 텍스트로 반환.
- **출력 스키마:** `end` 노드(없으면 위상 마지막 노드)의 `response_schema["200"]`를 outputSchema로 사용(있으면).
- **SDK/트랜스포트:** 공식 `mcp` Python SDK(`mcp.server`), Python 3.11 호환 버전 고정. 트랜스포트 stdio 기본, 필요 시 SSE. `backend/mcp_server.py`가 별도 진입점으로 동일 SQLite·`engine` 모듈 공유.

---

## 9. 모듈별 파일/디렉토리 소유권

> 한 파일은 **단일 소유자**. 공유가 불가피한 경계(예: `mcp_server.py`)는 주 소유자를 명시하고 상대는 import만.

| 경로 | 소유자 | 비고 |
|------|--------|------|
| `backend/app.py` | backend-engineer | FastAPI 앱, lifespan, 라우터 등록, StaticFiles/Jinja2 마운트 |
| `backend/db.py` | backend-engineer | SQLite 연결, §2 DDL idempotent 마이그레이션 |
| `backend/models.py` | backend-engineer | §5 Pydantic 모델 전부 (Node/Edge 포함 공유 모델의 정본) |
| `backend/repositories/*.py` | backend-engineer | specs/operations/workflows/nodes/edges/executions CRUD + JSON 직렬화 |
| `backend/routers/specs.py` | backend-engineer | `/api/specs*` |
| `backend/routers/workflows.py` | backend-engineer | `/api/workflows*` (run 포함) |
| `backend/routers/executions.py` | backend-engineer | `/api/executions*` (+ SSE 선택) |
| `engine/__init__.py` | mcp-engineer | export: run_workflow, parse_openapi, ParseResult |
| `engine/parser.py` | mcp-engineer | OpenAPI 2.0/3.x 파싱, $ref 해석, §3 출력 |
| `engine/executor.py` | mcp-engineer | §6 run_workflow, 위상정렬, data_mapping 주입, HTTP 호출 |
| `engine/http_client.py` | mcp-engineer | httpx 래퍼(타임아웃·인증 주입) |
| `backend/mcp_server.py` | mcp-engineer | §8 MCP 노출 진입점 (backend는 미수정) |
| `templates/**` | frontend-engineer | base/index/editor + partials |
| `static/canvas.js` | frontend-engineer | Drawflow ↔ §4 그래프 직렬화(`toContractGraph`/`fromContractGraph`) |
| `static/style.css` | frontend-engineer | |
| `requirements.txt` | backend-engineer | 의존성. mcp-engineer가 engine/MCP 의존 추가 시 PR로 통지 |
| `mcp_provider.db` | (런타임 생성) | 커밋 금지 |
| `_workspace/01_architect_contracts.md` | architect | 본 문서 |
| `_workspace/02_backend_status.md` | backend-engineer | 산출 |
| `_workspace/03_engine_status.md` | mcp-engineer | 산출 |
| `_workspace/04_frontend_status.md` | frontend-engineer | 산출 |

**공유 모델 정본 규칙:** `Node`/`Edge`/`WorkflowGraph`/`ExecutionResult`의 **Pydantic 정의 정본은 `backend/models.py`**. engine은 동일 키의 dict 또는 자체 동형 모델을 써도 되나 **와이어 키는 §4/§5.0과 100% 일치**해야 한다. frontend는 §4 JSON shape을 정본으로 삼는다.

---

## 10. 의존성 (Python 3.11 휠 존재 버전, 핀은 구현자가 확정)

- `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`(파일 업로드), `httpx`(URL fetch + 엔진 호출), `pydantic>=2`.
- 파서: `PyYAML`(+ 선택 `jsonref`/`prance`). 매핑: 표준 dict 접근 또는 `jsonpath-ng`.
- MCP: 공식 `mcp` SDK (3.11 호환 버전 고정).
- DB: 표준 `sqlite3`(권장, 단일파일·단순). 비동기 필요 시 `aiosqlite` 고려 가능.

---

## 11. 구현자 체크 — 가장 깨지기 쉬운 3대 경계

1. **그래프 JSON 키**: `node.id`(str), `operation_id`(int|null), `params.{path,query,header,body}`, `edge.{id,source,target,data_mapping}`, `data_mapping[].{from,to}`. → frontend `canvas.js` / engine `executor.py` / backend `models.py` 3자 일치.
2. **`from` 키 와이어 포맷**: 반드시 문자열 `"from"`. Pydantic alias(`from_`)는 내부 구현일 뿐.
3. **노드 실패 시 HTTP 200 + `status:"failed"`**: 로그 보존을 위해 엔진/REST 모두 준수.

---

*문서 버전: v1 (초안). 변경 이력은 이 섹션 하단에 append.*
