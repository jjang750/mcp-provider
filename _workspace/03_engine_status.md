# 03 — Engine Status (mcp-engineer)

> 소유: mcp-engineer. 계약(`01_architect_contracts.md`) §3/§4/§5.0/§6/§8 준수.
> 대상 런타임: **Python 3.11**. `engine/`는 FastAPI 비의존 순수 모듈. backend가 import해 호출.

## 1. 생성/소유 파일

| 경로 | 내용 |
|------|------|
| `engine/__init__.py` | export: `run_workflow`, `parse_openapi`, `ParseResult` |
| `engine/_models.py` | `ParseResult` 모델 (pydantic 있으면 BaseModel, 없으면 동형 shim — 바레 환경에서도 import 가능) |
| `engine/parser.py` | OpenAPI 2.0/3.0/3.1 파싱, `$ref` 해석, §3 출력 |
| `engine/executor.py` | §6 `run_workflow`, 위상정렬, data_mapping 주입, HTTP 호출, 로그 |
| `engine/http_client.py` | httpx 래퍼 (URL 빌드 + 경로파라미터 치환 + 인증 주입 + 타임아웃) |
| `backend/mcp_server.py` | §8 MCP 노출 진입점 (최소 동작 스텁 + 명확한 TODO, import는 항상 성공) |

## 2. 최종 시그니처 (backend가 라우터 연결 시 그대로 사용)

```python
# engine/parser.py
def parse_openapi(raw_content: str, source_hint: str | None = None) -> ParseResult: ...

class ParseResult:           # _models.py
    spec_version: Optional[str]
    base_url:     Optional[str]
    operations:   list[dict]   # 각 dict = OperationOut(§5.2)의 DB-비의존 부분
    warnings:     list[str]
```

```python
# engine/executor.py  (engine.run_workflow 로 export)
async def run_workflow(
    graph,                              # WorkflowGraph 모델 또는 동일 키 dict (§4/§5.0)
    initial_input: dict | None = None,  # start 노드 외부 입력
    auth: dict | None = None,           # {"token":..., "api_key":...} 실행시 주입(미저장)
    on_node_event=None,                 # Optional[Callable[[dict], None|Awaitable]] (SSE용; NodeLog dict 전달)
    *,
    operation_resolver: Callable[[int], dict | None] | None = None,  # ★ backend 필수 주입
    timeout: float = 30.0,
) -> dict: ...                          # §5.0 ExecutionResult 와 동일 키 dict
```

### ★ backend 필수 작업: `operation_resolver` 주입
`api_call` 노드는 `operation_id`(int, DB PK)만 들고 있고 method/path/base_url/auth를 모른다.
backend는 **operations 테이블을 조회해 operation dict(§3 shape)를 반환하는 콜백**을 만들어
`run_workflow(..., operation_resolver=resolver)`로 넘겨야 한다. resolver가 없거나 None을 반환하면
해당 노드는 깔끔히 실패 처리(`status:"failed"`)되고 실행은 죽지 않는다.

resolver가 반환해야 하는 키: `method, path, base_url, auth, request_schema`(content_type 읽음), (`params_schema`는 선택).
→ `OperationOut`/operations 행을 그대로 dict로 넘기면 충분.

## 3. 반환 키 (backend 저장·응답 시 그대로, 100% 일치 확인됨)

```jsonc
// run_workflow 반환 (== ExecutionResult §5.0)
{
  "execution_id": 0,          // ★ placeholder — backend가 DB INSERT 후 실제 PK로 덮어쓸 것
  "workflow_id": 7,           // graph.workflow_id 그대로 (없으면 0)
  "status": "success"|"failed",   // 엔진은 "running"을 반환하지 않음(동기 완주)
  "started_at": "<ISO-8601 UTC>",
  "finished_at": "<ISO-8601 UTC>",
  "result": <Any|null>,       // 종단(end 또는 마지막 성공) 노드 output, 실패 시 null
  "logs": [ NodeLog, ... ]
}
// NodeLog (== §5.0)
{ "node_key": str, "seq": int, "status": "success"|"failed"|"skipped",
  "input": dict|null, "output": Any|null, "error": str|null, "timestamp": "<ISO-8601 UTC>" }
```

### backend가 채워야 할 / 덮어쓸 필드
- **`execution_id`**: 엔진은 항상 `0`. backend가 `executions` 행 생성 후 실제 정수 PK로 교체.
- 그 외 키는 엔진이 완성해 제공 → backend는 `executions.result`(= 전체 ExecutionResult.result),
  `execution_logs`(각 NodeLog)로 매핑 저장. NodeLog의 `input/output`은 dict/Any → JSON `TEXT` 직렬화.
- **특수 로그 항목 주의**: 그래프 경고가 있으면 `seq=-1`, `node_key="__graph__"`, `status="success"`인
  정보성 로그가 logs[0]에 prepend된다. 순환 거부 시 `seq=0, node_key="__graph__", status="failed"` 단일 로그.
  backend는 `__graph__` 로그도 그대로 저장하거나(권장) 필터링 가능 — UI 표시는 frontend 판단.

## 4. data_mapping / JSONPath 구현 (§4 부분집합 — 100% 준수)
- 와이어 키는 문자열 **`"from"`** (pydantic alias `from_`도 관용 처리, §11).
- `from` 지원: `$`(output 전체), dotted(`$.a.b`), 인덱스(`$.a.items[0].name`). 선행 `$`/`$.` 유무 무관.
  필터/와일드카드 미지원 → `(found=False)` 반환.
- `to` 지원 prefix: `params.path.<k>`, `params.query.<k>`, `params.header.<k>`, `params.body`, `params.body.<k>...`.
- 적용 순서: 노드 정적 `params` → 들어오는 모든 edge data_mapping 순차 덮어쓰기.
- 매핑 실패(`from` 경로 없음)는 **노드를 죽이지 않고** 해당 노드 로그 `error`에 `warnings:`로 누적.

## 5. 노드 타입 동작
- `start`: output = `initial_input` (§7 기준점). 외부 입력 노출.
- `api_call`: `operation_resolver`로 op 조회 → http_client로 실제 호출. HTTP 4xx/5xx = 노드 실패(`error="HTTP {code}"`, output엔 응답 보존). 응답 JSON 파싱(아니면 `{"raw","status_code"}` 래핑, §0.4).
- `end`: output = 조립된 `params.body`, 없으면 단일 선행 노드 output 패스스루.
- `transform`: **v1은 패스스루**(코드 실행 없음). `params.body` 있으면 그것, 없으면 병합된 params. (TODO: 표현식 변환은 미구현.)
- 알 수 없는 타입: 노드 실패.

## 6. 지원 스펙 버전 / 파서 범위
- **OpenAPI 2.0 (swagger), 3.0.x, 3.1.x** 파싱. JSON + YAML(PyYAML) 둘 다. JSON 우선 시도 후 YAML.
- `$ref` 내부 참조 재귀 해석(`#/definitions/*` 2.0, `#/components/*` 3.x). 순환은 1회 펼친 뒤 `{"$circular":true}`.
- allOf 머지(properties/required 병합), oneOf/anyOf는 첫 후보 채택 + warning.
- base_url: 3.x `servers[0].url`(변수 default 치환), 2.0 `schemes+host+basePath`.
- 인증 매핑(§3): http bearer→`{"type":"bearer"}`, http basic/2.0 basic→`{"type":"basic"}`, apiKey→`{"type":"apiKey","in","name"}`, oauth2/oidc→`{"type":"oauth2"}`, 없음→`{"type":"none"}`. operation `security` > global `security`, 첫 requirement 채택.
- 파라미터: path/query/header 분류, cookie는 header 버킷에 `(cookie)` 표기. 2.0 `in:body`→request_schema, `in:formData`→urlencoded request_schema. path-level 공유 parameters 머지.
- operationId 없으면 `{method}_{path_slug}` 생성, 중복은 `_N` suffix로 유일화.
- 부분 실패 허용: operation 단위 try/except, 전부 `warnings`에 누적. 로드 자체 실패 시 빈 operations + warning.

검증됨(스모크): 공식 Petstore 형태 3.0/2.0 모두 GET/POST 2개 operation, $ref 펼침, 인증·base_url 정확.

## 7. MCP 노출 구현 상태 (`backend/mcp_server.py`)
- **순수 헬퍼는 완성·테스트됨**: `slugify`, `build_tool_name`(`workflow_{id}_{slug}`, §8), `build_input_schema`(start 노드 미충족 필수 파라미터 → JSON Schema, §8), `build_output_schema`(end/마지막 노드 `response_schema["200"]`).
- **스텁(=TODO, 1차 우선순위 낮음)**:
  - `load_exposed_workflows()` → 현재 `[]`. backend가 workflows/nodes/edges repo로 §4 graph dict 리스트 제공해야 함.
  - `make_operation_resolver()` → 현재 None 반환. backend operations repo 연결 필요.
  - `main()` → stdio MCP 서버. `mcp` SDK 미설치 시 명확한 SystemExit. SDK import surface(`mcp.server.Server`, `mcp.server.stdio.stdio_server`, `mcp.types`)는 공식 저수준 서버 기준 — **핀 버전에서 재확인 필요**.
- import은 SDK/DB 없이도 항상 성공(바레 환경 확인). backend는 이 파일 **수정하지 말 것**(§9). 같은 SQLite·engine 공유.

## 8. 필요한 requirements 의존성 (backend가 requirements.txt에 반영)
> requirements.txt는 backend 소유. 아래를 추가 요청한다(전부 Python 3.11 휠 존재).

- `httpx` — 엔진 HTTP 호출 (executor/http_client). **필수.**
- `PyYAML` — YAML 스펙 파싱 (parser). **필수**(미설치 시 YAML 스펙 거부, JSON은 동작).
- `mcp` — MCP 서버 노출 (mcp_server.py). 1차 선택(우선순위 낮음). Python 3.11 호환 버전 핀.
- `jsonpath-ng` — **불필요**. 계약 §4 부분집합을 자체 구현(`executor.resolve_path`)으로 충족. (원하면 추가 가능하나 미사용.)
- `jsonref`/`prance` — **불필요**. `$ref` 해석 자체 구현.
- `pydantic>=2` — backend가 이미 사용. 엔진은 optional import(없어도 import 가능하나, 운영 환경엔 항상 존재).

## 9. 알려진 미지원 / TODO
- `transform` 노드 실제 변환 로직 미구현(v1 패스스루). 표현식/매핑 변환 필요 시 추가.
- 병렬 분기 실행 없음(§0.3대로 위상 순차만).
- 외부 `$ref`(다른 파일/URL) 미해석 → `{"$unresolved": ref}` 표기.
- MCP 서버 DB 와이어링·SDK 버전 확인 미완(§7 TODO).
- `transform`/`end` 외 멀티 종단 노드의 `result`는 "마지막 성공 노드 output" 단일값(다중 출력 집계 없음).

## 10. 경계 통지 (frontend / qa 참고)
- 노드/엣지/그래프 JSON shape은 §4 그대로 사용 — 변경 없음. 캔버스 직렬화는 안전.
- frontend 폼은 operation `params_schema`(§3 평탄 구조: path/query/header 배열, 각 `name/type/required` + 선택 `enum/default/description`)를 그대로 렌더 가능.
- qa 통합검증 포인트: (a) `run_workflow` 반환 키 == `ExecutionResult`, (b) `execution_id=0` placeholder를 backend가 덮는지, (c) `"from"` 와이어 키, (d) 노드 실패 시 HTTP 200 + `status:"failed"`(엔진 반환은 dict, HTTP 코드는 backend 책임).
