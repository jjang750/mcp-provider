# 05 — QA Integration Report (mcp-provider)

> 작성: qa-integrator. 방법: 4대 경계 교차 비교 + 실제 실행(import 스모크 + FastAPI TestClient end-to-end + 엔진 직접 호출).
> 대상 런타임 검증 환경: **Python 3.11.9**, fastapi 0.115.6, httpx/pydantic/PyYAML 설치 확인됨.
> 기준: `01_architect_contracts.md` (계약 = 정합성 판정 기준).

---

## 0. 경계별 PASS/FAIL 요약

| 경계 | 대상 | 판정 | 핵심 사유 |
|------|------|:----:|-----------|
| **경계3** | backend ↔ engine (`run_workflow` 시그니처) | **FAIL (critical)** | backend가 `operation_resolver`를 **미주입** → 모든 `api_call` 노드 실패. 런타임으로 증명. |
| **경계1** | backend ↔ frontend (페이지 라우트/REST) | **FAIL (critical)** | `GET /editor/{id}` 페이지 라우트 **부재** → 워크플로우 생성/목록의 링크가 전부 dead link. REST API 경로·shape은 일치. |
| **경계2** | 캔버스 JSON ↔ engine 노드/엣지 스키마 | **PASS** | `toContractGraph` 출력 / `models.py` / `executor.py` 3자 키 일치. `"from"` 와이어 키·`params.*` prefix·`$` 기준점 round-trip 보존(런타임 확인). |
| **경계4** | MCP 노출 ↔ 워크플로우 모델 | **PARTIAL (미완, 우선순위 낮음)** | `backend/mcp_server.py` import OK·순수 헬퍼 완성. 단 `load_exposed_workflows()`/`make_operation_resolver()`는 스텁(`[]`/`None`), `POST /api/workflows/{id}/expose` 엔드포인트 미구현. 1차 범위상 OK로 간주, 기록만. |

---

## 1. 실제 실행 검증 결과

1. **import 스모크 — PASS**
   - `from backend.app import app` → OK
   - `import engine` → OK, `engine.__all__ == ['run_workflow','parse_openapi','ParseResult']` (계약 §6 export 3종 모두 존재)
2. **의존성 — PASS(환경)**: requirements.txt의 핵심(fastapi/httpx/pydantic/PyYAML) 모두 설치됨. 추가 설치 불필요.
3. **end-to-end 스모크 (FastAPI TestClient, lifespan 포함) — 부분 PASS / critical 결함 노출**
   - `POST /api/specs/upload` (Petstore 3.0 최소본, GET findByStatus + POST addPet): **200**, `operation_count=2`, `base_url` 정확, `params_schema` 평탄구조(`{path:[],query:[{name,type,required,enum}],header:[]}`) 정상 → **PASS**
   - `POST /api/workflows` → 200, `PUT /api/workflows/{id}` (start→GET→POST→end + data_mapping) → 200 → **PASS**
   - **round-trip(`GET /api/workflows/{id}`)**: `edge_0.data_mapping == [{"from":"$.status","to":"params.query.status"}]` — **`"from"` 와이어 키 보존**, `node_1.params.query == {"status":"available"}` 정적값 보존 → **PASS**
   - `POST /api/workflows/{id}/run` → **HTTP 200, `status:"failed"`**. 로그:
     ```
     seq=0 node_0 start    success
     seq=1 node_1 api_call failed   error="no operation_resolver provided to engine"   ← critical
     seq=2 node_2 api_call skipped  "skipped due to upstream failure"
     seq=3 node_3 end      skipped
     ```
   - **결론: backend 스모크의 `status:"failed"`는 "테스트 그래프 의도"가 아니라 resolver 미주입으로 인한 전(全) api_call 실패였다.** 우려 A 가설 = 사실로 확정.
4. **엔진 resolver 경로 격리 검증 — PASS(엔진 측 정상)**
   - `run_workflow(graph, operation_resolver=<dict 반환 콜백>, timeout=3)` 직접 호출 시:
     ```
     node_1 api_call failed  error="GET https://petstore.invalid.example/pet/findByStatus failed: [Errno 11001] getaddrinfo failed"
     ```
   - resolver를 넘기면 엔진이 op를 정상 조회 → **실제 HTTP 호출까지 진행**(가짜 호스트라 DNS 실패만 남음). 즉 엔진은 계약대로 동작하며, **결함은 전적으로 backend의 주입 누락**임을 증명. resolver가 반환해야 할 dict 키(`method/base_url/path/auth/request_schema`)는 `OperationOut.model_dump()`와 100% 호환.

> 환경 제약: 외부 실제 API는 호출 불가(sandbox). 그러나 resolver 주입 여부·그래프 round-trip·shape 일치는 코드+런타임으로 모두 검증 완료.

---

## 2. 발견 버그

### 🔴 BUG-1 (critical) — `operation_resolver` 미주입 [경계3: backend ↔ engine]

모든 `api_call` 노드가 실행 즉시 실패한다. MVP 핵심 시나리오(실제 API 호출) 자체가 불가능.

**경계 양쪽 증거**

- engine 기대 (필수, keyword-only):
  `engine/executor.py:359-366`
  ```python
  async def run_workflow(graph, initial_input=None, auth=None, on_node_event=None, *,
                         operation_resolver: Optional[OperationResolver] = None,
                         timeout: float = http_client.DEFAULT_TIMEOUT_SECONDS) -> dict:
  ```
  `engine/executor.py:311-312` (resolver 없으면 즉시 실패)
  ```python
  if operation_resolver is None:
      return None, "no operation_resolver provided to engine"
  ```
- backend 제공 (누락):
  `backend/engine_bridge.py:49-66` — `run_workflow` 래퍼가 `operation_resolver` 파라미터도, 전달도 하지 않음:
  ```python
  async def run_workflow(graph, initial_input=None, auth=None, on_node_event=None):
      ...
      result = await fn(graph, initial_input=initial_input, auth=auth, on_node_event=on_node_event)
      # operation_resolver 인자 없음
  ```
  `backend/routers/workflows.py:83-87` — 호출부도 resolver 미전달:
  ```python
  result = await engine_bridge.run_workflow(graph, initial_input=body.initial_input, auth=body.auth)
  ```
- 런타임 증거: §1.3 로그 `error="no operation_resolver provided to engine"`.

**담당: backend.** **수정 내용:**
1. `backend/engine_bridge.py::run_workflow`에 `operation_resolver: Optional[Callable[[int], dict|None]] = None` 파라미터 추가하고 `fn(..., operation_resolver=operation_resolver)`로 전달.
2. `backend/routers/workflows.py::run_workflow` 핸들러에서 resolver 콜백을 만들어 주입. 이미 존재하는 `specs_repo.get_operation(conn, operation_pk) -> OperationOut|None`를 활용:
   ```python
   from ..repositories import specs as specs_repo
   def _resolver(op_id: int):
       op = specs_repo.get_operation(conn, op_id)
       return op.model_dump() if op is not None else None
   result = await engine_bridge.run_workflow(
       graph, initial_input=body.initial_input, auth=body.auth,
       operation_resolver=_resolver,
   )
   ```
   - resolver는 **dict**를 반환해야 함(엔진이 `op.get(...)` 사용). `OperationOut`은 `.model_dump()`로 변환. 반환 dict 키 `method/base_url/path/auth/request_schema`는 엔진 기대와 일치 확인됨(§1.4).
   - 검증 완료: resolver 주입 시 엔진이 op 조회→실제 HTTP 호출까지 진행.

---

### 🔴 BUG-2 (critical) — `GET /editor/{id}` 페이지 라우트 부재 [경계1: backend ↔ frontend]

워크플로우를 생성하거나 목록에서 클릭하면 이동하는 에디터 페이지가 404. 캔버스/에디터 전체가 도달 불가능.

**경계 양쪽 증거**

- frontend 가정:
  `templates/index.html:55` — 목록 링크 `href="/editor/${wf.id}"`
  `templates/index.html:85` — 생성 후 `window.location.href = '/editor/' + wf.id`
  `templates/editor.html:5-6` — `<meta name="workflow-id" content="{{ workflow_id }}" />` (컨텍스트 변수 `workflow_id` 주입 가정)
  `04_frontend_status.md:128-131` — "backend가 `GET /editor/{id}` → editor.html (`workflow_id` 주입) 제공 가정, 확인 필요"로 플래그됨.
- backend 실제:
  `backend/app.py:73-84` — `GET /` (index.html)만 존재. `GET /editor/{id}` 라우트 **없음**. (specs/workflows/executions 라우터는 전부 `/api/*` prefix이므로 페이지 라우트 미포함.)

**담당: backend.** **수정 내용:** `backend/app.py`에 에디터 페이지 라우트 추가(`templates` 객체 이미 존재):
```python
@app.get("/editor/{workflow_id}", tags=["meta"])
def editor(workflow_id: int, request: Request):
    if (_TEMPLATES_DIR / "editor.html").exists():
        return templates.TemplateResponse(
            "editor.html", {"request": request, "workflow_id": workflow_id}
        )
    return JSONResponse({"app": "mcp-provider", "editor": workflow_id})
```
- 컨텍스트 변수명은 frontend가 기대하는 **`workflow_id`** 정확히 사용(meta·title `{{ workflow_id }}` 양쪽 사용).

---

### 🟡 BUG-3 (minor) — 로드된 api_call 노드 파라미터 폼 재구성 불가 [경계1, 기능 한계]

frontend 자체 플래그(`04_frontend_status.md:133-141`, `#2`). `WorkflowDetail.nodes[]`에 `operation` 메타(params_schema 등)가 없어, 저장 후 재진입 시 api_call 노드의 동적 파라미터 폼을 다시 못 그린다(저장된 `params` 값은 보존·재저장됨). `canvas.js:239`가 로드 시 `operation: null`로 둠.

**담당: frontend (또는 backend 보조).** **수정 내용:** 1차 범위에서는 "신규 배치 시에만 폼 렌더, 로드 후엔 값 보존"으로 한정 — 계약 위반 아님(REST 계약상 node에 operation 메타 미포함이 정상). 개선 시 frontend가 `node.operation_id`로 `GET /api/specs/{spec_id}/operations`를 재조회하거나(현재 미사용 엔드포인트, 구현되어 있음), backend가 node 응답에 operation 메타를 옵션 포함. **1차 MVP 차단 아님 → 백로그.**

---

## 3. 추가 정합성 점검 (이상 없음 — 통과 항목)

- **`"from"` 와이어 키**: `models.py:46` `Field(alias="from")` + `populate_by_name`, `workflows.py:222` 저장 시 `{"from": m.from_}`, `executor.py:75-79` `_mapping_from`이 `"from"` 우선·`"from_"` 관용, `canvas.js:190/494` `{from,to}` 생성 → **4자 일치**. round-trip 런타임 보존 확인.
- **노드/엣지 키**: `id/type/label/operation_id/params{path,query,header,body}/position{x,y}` 및 `id/source/target/data_mapping` — 계약 §4와 `models.py`·`canvas.js`·`executor._normalise_graph` 전부 일치.
- **`to` prefix 의미**: `executor._set_input_path`(executor.py:131-161)가 `params.path|query|header.<k>`, `params.body`, `params.body.<k>` 모두 처리. frontend 안내 문구(canvas.js:467-469)와 일치.
- **status 값 집합**: execution `running|success|failed`, NodeLog `success|failed|skipped` — `models.py` Literal / engine 반환 / SQLite CHECK / frontend `renderExecution`(canvas.js:533) 모두 일치.
- **노드 실패 = HTTP 200 + `status:"failed"`**: `workflows.py:103-104` 주석대로 200 반환, frontend는 `res.ok`가 아닌 `result.status`로 분기(canvas.js:347-352). 준수.
- **`execution_id=0` placeholder 덮어쓰기**: engine 반환 `execution_id:0`(executor.py:513) → backend `workflows.py:100-101`에서 실제 PK로 교체 후 `persist_result`. 정상.
- **응답 필드 직렬화**: JSON 컬럼이 응답에서 파싱 객체로 노출됨(repo가 `loads`). camelCase/snake_case 혼선 없음(전부 snake_case 일관).
- **Pydantic 필수 필드**: frontend가 보내는 PUT body(`{nodes,edges}`)·RunRequest(`{initial_input,auth}`)·CreateRequest(`{name,description?}`) 모두 모델 필수 필드 충족.
- **Python 3.11 호환**: `list[X]`/`dict[X,Y]`/`X|None` 어노테이션은 전부 `from __future__ import annotations` 하에 문자열화되어 3.11 안전. 3.12+ 전용 문법(예: PEP 695 `type` 별칭, 제네릭 `class C[T]`) **미발견**. `engine/_models.py`는 pydantic 부재 시 shim 폴백까지 갖춤.
- **`__graph__` 특수 로그**: engine이 topo 경고 시 `seq=-1, node_key="__graph__"` prepend(executor.py:497-509). backend `persist_result`는 그대로 저장(필터 안 함), frontend는 그대로 렌더 — 계약 §3(03 status) 권장과 일치. (DB `execution_logs.seq`에 음수 저장 허용됨 — 스키마 제약 없음, 문제 없음.)

---

## 4. 재검증 필요 항목 (수정 후)

1. **BUG-1 수정 후**: 동일 e2e 스크립트로 `/run` 재실행 → api_call 노드가 "resolver 없음"이 아니라 실제 HTTP 단계까지 진행하는지 확인. (가능하면 로컬 목 서버로 200 응답 경로까지 1회 통과.)
2. **BUG-2 수정 후**: `GET /editor/1` → 200 + editor.html 렌더, `<meta name="workflow-id">`에 id 주입되는지 확인. `GET /` index 링크 → editor 왕복.
3. **경계4(MCP)**: 1차 우선순위 낮음. 추후 `mcp_server.py`의 `make_operation_resolver()`/`load_exposed_workflows()` 와이어링 시 BUG-1과 동일한 resolver 패턴을 공유하는지 재검증(중복 구현 방지).

---

## 5. 수정 배정 요약

| 버그 | 심각도 | 담당 | 수정 한 줄 요약 |
|------|:------:|------|------------------|
| BUG-1 | critical | **backend** | `engine_bridge.run_workflow`에 `operation_resolver` 파라미터 추가·전달 + `routers/workflows.py`에서 `specs_repo.get_operation().model_dump()` 콜백 주입 |
| BUG-2 | critical | **backend** | `app.py`에 `GET /editor/{workflow_id}` 라우트 추가(컨텍스트 `workflow_id`) |
| BUG-3 | minor | frontend | (백로그) 로드된 api_call 노드 폼 재구성 — `operation_id`로 operations 재조회 |

> qa-integrator는 검출·검증·보고에 집중. BUG-1/2는 단일 1줄 수준을 넘는 로직(콜백 구성)이라 직접 수정하지 않고 backend 담당에 배정. 명백한 오타·import 경로 결함은 발견되지 않았다(수정 적용 없음).

---

# 기능추가 검증 — 엣지 data_mapping UX 개선 (응답 필드 클릭 삽입 + auto-map)

> 작성: qa-integrator. 대상 변경: `static/canvas.js`, `static/style.css`, `templates/partials/node_params.html`(frontend), `backend/routers/operations.py`(신규), `backend/app.py`(라우터 등록).
> 검증 환경: Python 3.11.9, node v22.14.0. 기준: `01_architect_contracts.md` §3/§4/§5.2 + `02_backend_status.md` FEAT-1.

## 경계별 PASS/FAIL 요약

| 경계 | 대상 | 판정 | 핵심 사유 |
|------|------|:----:|-----------|
| **A** | frontend ↔ backend `/api/operations/{operation_id}` | **PASS** | 라우터 등록·경로 정확(`prefix="/api/operations"`, prefix 오염 없음). `response_model=OperationOut` → shape 보증. canvas가 읽는 `params_schema`/`response_schema`/`request_schema` 모두 응답에 존재. 업로드→GET 200, 없는 id→404 런타임 확인. |
| **B** | 스키마→경로 추출 / auto-map 정확성 | **PASS** | object→`$.<key>`, array(items object)→`$[0].<key>`(`[*]` 미생성), `to`는 `params.{path,query,header,body}` prefix 100% 준수. auto-map 정확 일치 우선·1 target 1회. Node 스니펫 13건 전부 통과. |
| **C** | 회귀: round-trip & 실행 | **PASS** | `toContractGraph`/`fromContractGraph`/`edgeMappings`/와이어 키 `"from"`/`"to"` 불변. e2e: 업로드→생성→PUT(data_mapping)→GET round-trip(`"from"` 보존)→`/run` resolver 정상 도달(HTTP 503=환경). |

## 실제 실행 검증 결과

1. **import / 라우트 — PASS**
   - `python -c "from backend.app import app"` → **IMPORT_OK**.
   - 라우트 목록에 `/api/operations/{operation_id}` 존재(True), 기존 `/api/specs/{spec_id}/operations`와 별개로 공존(prefix 충돌 없음).
   - `python -m py_compile backend/routers/operations.py backend/app.py` → **OK**. Python 3.12+ 문법(신형 제네릭/`match`/`type` 문) 혼입 없음. `from __future__ import annotations` 사용, 3.11 호환.
2. **`node --check static/canvas.js` — PASS** (CANVAS_OK).
3. **경계 A e2e (TestClient, lifespan 포함) — PASS**
   - `POST /api/specs/upload`(OpenAPI 3.0, GET /get + POST /post) → 200, `operation_count=2`.
   - `OperationOut` 키 = `{id, spec_id, operation_id, method, path, base_url, summary, params_schema, request_schema, response_schema, auth}` — canvas `ensureOperation`가 읽는 필드 전부 포함.
   - `GET /api/operations/{id}` → **200**, `params_schema`/`response_schema`/`request_schema` 포함.
   - `GET /api/operations/999999` → **404** `{"detail":"Operation 999999 not found."}`.
4. **경계 B 단위 검증 (node 스니펫, canvas.js 함수 그대로 재현) — 13/13 PASS**
   - object response → `$.id`,`$.name` / array items object → `$[0].id`,`$[0].status` (그리고 `[*]` 미포함 명시 검증) / 201·default fallback / no-schema→`[]`.
   - param paths(path/query/header/body.props), body-no-props→`params.body`, 모든 to-path가 `params.` prefix.
   - auto-map: 정확 일치(name↔name), 느슨(id↔petId), 무매칭→`[]`, 각 to 1회만 사용.
5. **경계 C e2e 회귀 — PASS**
   - `PUT /api/workflows/{id}` (start→GET→POST→end, edge_1에 `{"from":"$.id","to":"params.body.name"}`) → 200.
   - round-trip `GET`: edge data_mapping = `[{"from":"$.id","to":"params.body.name"}]` — **`"from"` 와이어 키 보존**, `has 'from' key: True`.
   - `node_1.params.query == {"q":"hi"}` 정적값 보존.
   - `POST /run` → HTTP 200 `status:"failed"`, 로그 seq0 start=success / seq1 api_call=failed(`HTTP 503`) / seq2,3=skipped. **resolver가 op를 정상 반환→엔진이 실제 HTTP 호출 단계 도달**(503은 샌드박스 아웃바운드 차단=환경 원인, 코드 결함 아님). 이전 동작과 동일.

## 추가 점검 결과

- **Fallback (response_schema/params_schema 비었을 때 수동 입력)** — **PASS**. `drawEdgeMapping`(canvas.js 702–713): `!hasFrom && !hasTo`면 안내 힌트 출력 후에도 `#mapping-rows` + "+ 매핑 추가" 버튼을 **항상** 렌더 → 기존 빈 텍스트 직접 입력 행 유지. 한쪽만 없을 때도 "스키마 없음 — 직접 입력하세요" + 반대쪽 칩 + 수동 행 공존.
- **renderNodeParams 로드 후 복원** — `d.operation`이 null이고 `operation_id`가 있으면 캐시 hit 시 즉시, miss 시 `ensureOperation`(`GET /api/operations/{id}`)로 메타 복원 후 폼 재렌더. 과거 "알려진 한계 #2"(로드 후 폼 재구성 불가) 개선됨. `operationPending`으로 중복 fetch 방지.
- **dead link / 필드명 불일치** — 없음. canvas의 fetch URL `${API}/operations/${operationId}` = `/api/operations/{id}` 백엔드 라우트와 일치. 응답 필드명(`response_schema`/`params_schema`/`request_schema`/`id`)이 `OperationOut`와 100% 일치(snake_case 양쪽 동일).
- **`escapeAttr`/`escapeHtml`** — 칩/팔레트에서 사용, 둘 다 정의됨(utils). XSS 회피 일관.

## 발견 버그

**없음.** critical/major/minor 모두 0건. 신규 경계 A·B·C 전부 PASS, 회귀 없음.

- 와이어 키(`"from"`/`"to"`), Node 직렬화 shape(§4), `edgeMappings` 구조(`[{from,to}]`), `collectMapping`/`persistMapping`/`toContractGraph`/`fromContractGraph` 모두 불변 확인 — FEAT는 입력 `.value`를 채우는 UX 레이어로만 동작, 직렬화 경로 미변경.
- 직접 수정 사항 없음(오타·import 결함 미발견).

> 결론: 기능추가 "엣지 data_mapping UX 개선" — **경계 A/B/C 전부 PASS, 회귀 없음, 버그 0건.** 환경 원인(샌드박스 아웃바운드 503)은 코드 결함과 명확히 구분됨(resolver 호출까지 정상 도달).
