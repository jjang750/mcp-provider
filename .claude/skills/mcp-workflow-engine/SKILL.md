---
name: mcp-workflow-engine
description: 노드/엣지 그래프의 순차 실행 엔진과 MCP 서버 노출 구현 방법. 위상정렬 실행, 엣지 data_mapping을 통한 노드 간 데이터 전달, 실제 HTTP API 호출, 실행 로그, 완성 워크플로우의 MCP 도구 노출을 다룬다. "실행 엔진", "워크플로우 실행", "노드 순차 실행", "엣지 데이터 전달", "MCP 서버 노출", "MCP 도구 등록" 작업 시 반드시 사용. mcp-engineer 전용.
---

# MCP 워크플로우 실행 엔진 + MCP 서버 노출

노드/엣지 그래프를 받아 순차 실행하고, 완성된 워크플로우를 MCP 도구로 노출한다. 데이터 모델은 `mcp-provider-architecture` 계약을 따른다.

## 왜 순수 모듈로 작성하나
backend(FastAPI)가 import해서 호출하고, MCP 서버도 호출한다. FastAPI 의존성에 묶이면 재사용 불가. 엔진은 **입력(그래프+초기입력) → 출력(결과+로그)**의 순수 함수/클래스로 작성한다.

## 실행 엔진 설계

### 진입 인터페이스 (backend·MCP 공유)
```python
async def run_workflow(
    graph: WorkflowGraph,          # nodes + edges
    initial_input: dict,           # start 노드 외부 입력
    on_node_event=None,            # 진행상황 콜백(SSE용), optional
) -> ExecutionResult: ...
```
backend는 이 시그니처를 `_workspace/03_engine_status.md`에서 읽어 라우터를 연결한다.

### 실행 절차
1. **그래프 검증** — 순환 감지(DFS), 고립 노드 경고, start/end 존재 확인.
2. **위상정렬** — 엣지 기준 실행 순서 결정. 순차 실행(병렬은 v1 범위 외, 단순 유지).
3. **노드별 실행:**
   - 입력 조립: 들어오는 엣지의 `data_mapping`으로 선행 노드 출력 → 현재 노드 params에 주입.
   - `api_call` 노드: operation의 method/path/base_url + 주입된 params로 실제 HTTP 요청(`httpx.AsyncClient`). 인증 토큰 주입.
   - 응답 파싱 → 노드 출력으로 저장 (다음 노드가 참조).
   - `execution_logs`에 input/output/status/error 기록.
4. **실패 처리:** 노드 실패 시 해당 지점에서 중단, 실패 노드·원인을 로그에 남기고 부분 결과 반환. 데이터 삭제 금지.

### 데이터 매핑 적용
edge.data_mapping의 각 항목 `{"from": "$.output.id", "to": "params.path.userId"}`:
- `from`: 선행 노드 출력에 대한 JSONPath (`jsonpath-ng` 또는 단순 dotted 접근).
- `to`: 현재 노드 params 내 목적지 경로.
- 매핑 누락 시 노드 기본 params 사용. 필수 파라미터 미충족이면 명확한 오류.

## MCP 서버 노출

### 도구 등록
- 공식 `mcp` Python SDK 사용 (`mcp.server`). Python 3.11 호환 버전 고정.
- `mcp_exposed=true` 워크플로우마다 도구 1개 등록:
  - 도구명: `workflow_{id}_{slug}`
  - inputSchema: start 노드가 요구하는 외부 입력(JSON Schema)
  - 핸들러: `run_workflow(graph, initial_input=tool_args)` 호출 → 결과 반환
- 트랜스포트: stdio(로컬 클라이언트) 기본, 필요 시 SSE.

### 서버 실행 형태
- backend 앱과 별도 진입점(예: `backend/mcp_server.py`)으로 실행 가능하게 한다. 동일 SQLite·엔진 모듈 공유.

## 엣지 케이스 체크리스트
- [ ] 순환 그래프 → 실행 거부 + 명확한 메시지
- [ ] 도달 불가 노드(고립) → 경고 후 스킵
- [ ] HTTP 4xx/5xx 응답 → 노드 실패로 기록(엔진 자체는 죽지 않음)
- [ ] 타임아웃 → httpx timeout 설정 + 실패 처리
- [ ] 인증 토큰 부재 → 실행 전 사전 검증
- [ ] 노드 출력이 다음 노드 기대 타입과 불일치 → 매핑 단계에서 경고

## 출력
`_workspace/03_engine_status.md`에 `run_workflow` 시그니처, 지원 노드 타입, MCP 노출 방식, 실행 로그 shape을 기록 (backend·frontend·qa가 참조).
