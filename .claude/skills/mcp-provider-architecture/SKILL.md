---
name: mcp-provider-architecture
description: mcp-provider의 정규 데이터 모델·계약 포맷을 정의. architect가 시스템 설계 시 SQLite 스키마, 노드/엣지 JSON 스키마, REST API 계약, MCP 노출 전략을 일관되게 산출하도록 한다. "아키텍처 설계", "데이터 모델", "스키마 설계", "계약 정의", "전체 구조 설계" 요청 시 사용. 매 실행마다 스키마가 달라지면 구현 팀이 깨지므로 반드시 이 기준을 따른다.
---

# MCP Provider 아키텍처 — 정규 계약

이 스킬은 mcp-provider의 **공유 계약**을 표준화한다. architect는 이 기준 위에서 설계하며, 산출물을 `_workspace/01_architect_contracts.md`에 기록한다. 구현 팀(backend/mcp/frontend)은 이 계약을 단일 진실 공급원으로 삼는다.

## 왜 표준 계약이 필요한가
세 모듈이 병렬로 구현되므로, 데이터 shape이 모듈마다 어긋나면 경계면 버그가 폭발한다. 계약을 먼저 고정하면 각자 독립 구현해도 결합 시 맞아떨어진다. 매 세션 스키마가 흔들리면 기존 워크플로우와 호환이 깨진다 — 그래서 변경은 신중히, 이유와 함께.

## 시스템 개요
```
[사용자] → htmx/Jinja2 UI → FastAPI(backend) → SQLite
                              ↓ import
                    mcp-engine (파서/실행엔진/MCP서버)
                              ↓ 실행 시
                    실제 외부 HTTP API 호출
[외부 MCP 클라이언트] ← MCP 서버 노출 ← 완성된 워크플로우
```

## 정규 SQLite 데이터 모델
최소 테이블. 단일 SQLite 파일. 변경 시 idempotent 마이그레이션.

| 테이블 | 핵심 컬럼 | 설명 |
|--------|----------|------|
| `specs` | id, name, source_type(file/url), raw_content, parsed_at | 업로드된 OpenAPI 스펙 원본 |
| `operations` | id, spec_id(FK), operation_id, method, path, summary, params_schema(JSON), request_schema(JSON), response_schema(JSON), auth(JSON) | 스펙에서 추출된 API 오퍼레이션(= 노드 후보) |
| `workflows` | id, name, description, created_at, updated_at, mcp_exposed(bool) | 사용자가 만든 워크플로우 |
| `nodes` | id, workflow_id(FK), operation_id(FK nullable), type, label, params(JSON), position_x, position_y | 캔버스 노드 |
| `edges` | id, workflow_id(FK), source_node_id, target_node_id, data_mapping(JSON) | 노드 연결 + 데이터 매핑 |
| `executions` | id, workflow_id(FK), status, started_at, finished_at, result(JSON) | 실행 세션 |
| `execution_logs` | id, execution_id(FK), node_id, status, input(JSON), output(JSON), error, timestamp | 노드별 실행 로그 |

> 컬럼 추가는 자유, 위 핵심 컬럼은 보존. JSON 컬럼은 TEXT에 직렬화.

## 정규 노드/엣지 JSON 스키마
프론트 Drawflow 직렬화와 엔진 파서가 공유하는 shape. **이 shape이 가장 자주 깨지는 경계** — 엄격히 지킨다.

```jsonc
// Node
{
  "id": "node_1",
  "type": "api_call",            // api_call | start | end | transform
  "label": "GET /users",
  "operation_id": 42,             // operations 테이블 FK (api_call일 때)
  "params": {                     // 사용자가 채운 파라미터 값/매핑
    "path": {"userId": "{{node_0.output.id}}"},
    "query": {},
    "header": {},
    "body": {}
  },
  "position": {"x": 120, "y": 80}
}

// Edge
{
  "id": "edge_1",
  "source": "node_0",
  "target": "node_1",
  "data_mapping": [               // 이전 노드 출력 → 다음 노드 입력 매핑
    {"from": "$.output.id", "to": "params.path.userId"}
  ]
}

// Workflow graph (저장/로드 단위)
{
  "workflow_id": 7,
  "nodes": [ /* Node[] */ ],
  "edges": [ /* Edge[] */ ]
}
```

**데이터 매핑 표기:** 노드 출력 참조는 `{{node_id.output.<jsonpath>}}` 템플릿 또는 edge의 `data_mapping` 배열. 둘 중 엔진이 지원하는 방식을 architect가 택일하여 계약에 명시한다(권장: edge `data_mapping` 단일 방식으로 통일).

## 정규 REST API 계약 (예시 — architect가 확정)
| Method | Path | 설명 |
|--------|------|------|
| POST | `/api/specs/upload` | 파일 업로드 → 파싱 → operations 저장 |
| POST | `/api/specs/from-url` | URL fetch → 파싱 → 저장 |
| GET | `/api/specs/{id}/operations` | 스펙의 오퍼레이션(노드 후보) 목록 |
| GET/POST | `/api/workflows` | 워크플로우 목록/생성 |
| GET/PUT/DELETE | `/api/workflows/{id}` | 단건 조회/그래프 저장/삭제 |
| POST | `/api/workflows/{id}/run` | 순차 실행 트리거 |
| GET | `/api/executions/{id}` | 실행 상태/로그 (SSE 가능) |
| POST | `/api/workflows/{id}/expose` | MCP 도구로 노출 토글 |

모든 요청/응답은 Pydantic 모델로 정의. architect는 각 모델의 필드를 계약 문서에 코드로 명시한다.

## MCP 노출 전략
- 각 `mcp_exposed=true` 워크플로우 → 1개 MCP 도구.
- 도구명: `workflow_{id}_{slugified_name}`.
- 입력 스키마: 워크플로우의 `start` 노드가 요구하는 외부 입력 파라미터 집합.
- 출력: `end` 노드 또는 마지막 노드의 출력.
- 공식 `mcp` Python SDK 사용, stdio 또는 SSE 트랜스포트.

## Python 3.11 호환 규칙
- `match`문 OK. `|` union 타입 OK. `tomllib` OK.
- 3.12+ 전용 문법 금지(예: PEP 695 `type` 별칭, generic 클래스 신문법).
- 의존성은 3.11 휠이 존재하는 버전으로 고정.

## architect 산출 체크리스트
계약 문서(`_workspace/01_architect_contracts.md`)에 다음이 모두 있어야 한다:
- [ ] Assumptions 섹션
- [ ] 확정된 SQLite 스키마 (CREATE TABLE 수준)
- [ ] 노드/엣지/workflow JSON shape (위 정규형 기반, 확정)
- [ ] REST 엔드포인트 목록 + Pydantic 모델 필드 명시
- [ ] MCP 도구 매핑 규칙
- [ ] 모듈별 파일/디렉토리 소유권 표
- [ ] 데이터 매핑 방식 택일(template vs data_mapping) 명시
