---
name: fastapi-sqlite-backend
description: Python 3.11 호환 FastAPI + SQLite 백엔드 구조화 방법. 앱 부트스트랩, 리포지토리 패턴 영속화, idempotent 마이그레이션, 스펙 업로드(파일/URL) 처리, 워크플로우/노드/엣지 CRUD 엔드포인트를 다룬다. "FastAPI 백엔드", "SQLite 영속화", "엔드포인트 구현", "파일 업로드 API", "DB 모델", "리포지토리" 작업 시 반드시 사용. backend-engineer 전용.
---

# FastAPI + SQLite 백엔드

영속화 계층과 HTTP API를 구현한다. 스키마·엔드포인트 계약은 `mcp-provider-architecture`(architect 산출 `_workspace/01_architect_contracts.md`)를 따른다.

## 왜 경계를 지키나
파싱·실행 로직은 mcp-engineer 소유다. 백엔드는 그 순수 모듈을 **import해서 호출**만 한다. 경계를 침범하면 두 에이전트가 같은 코드를 다르게 작성해 충돌한다.

## 권장 구조 (Python 3.11)
```
backend/
├── app.py                 # FastAPI 앱, 라우터 등록, lifespan
├── db.py                  # SQLite 연결, 마이그레이션(idempotent)
├── models.py              # Pydantic 요청/응답 모델 (계약 기반)
├── repositories/          # 테이블별 CRUD (specs, workflows, nodes, edges, executions)
├── routers/               # specs.py, workflows.py, executions.py
└── mcp_server.py          # MCP 노출 진입점 (mcp-engineer와 공유)
```
- ORM은 선택: 표준 `sqlite3` + 얇은 리포지토리, 또는 `SQLModel`/`SQLAlchemy 2.x`. 단순함 우선 — 단일 파일 SQLite.
- `sqlite3` 사용 시 `check_same_thread=False` + 커넥션 관리 주의(FastAPI async). 비동기면 `aiosqlite` 고려.

## 마이그레이션 (idempotent)
- 앱 시작 시 `CREATE TABLE IF NOT EXISTS ...` 실행. 재실행 안전.
- 컬럼 추가는 `PRAGMA table_info`로 존재 확인 후 `ALTER TABLE`.

## 스펙 업로드 처리
- **파일:** `UploadFile`로 받아 content-type/확장자 검증(.json/.yaml/.yml). 크기 제한. 원본을 `specs.raw_content`에 저장.
- **URL:** `httpx`로 fetch, 타임아웃·리다이렉트 제한, content 검증. SSRF 주의(내부망 주소 차단 고려).
- 저장 후 mcp-engineer 파서 호출 → operations 저장. 파서가 반환한 `warnings[]`를 응답에 포함.

## 엔드포인트 구현 원칙
- 계약의 Pydantic 모델을 정확히 사용. 응답 shape을 임의 변경하면 프론트가 깨진다 — 변경 시 SendMessage로 frontend에 통지.
- 워크플로우 저장(`PUT /api/workflows/{id}`): 프론트 캔버스가 보낸 `{nodes, edges}` JSON을 nodes/edges 테이블에 트랜잭션으로 저장(기존 삭제 후 삽입 또는 diff).
- 실행(`POST /api/workflows/{id}/run`): 그래프 로드 → `run_workflow(...)` 호출 → execution 생성. 진행상황은 `GET /api/executions/{id}` 폴링 또는 SSE.
- 모든 에러는 적절한 status code + `{"detail": ...}`.

## 실행 상태 스트리밍 (SSE)
- 프론트가 실행 진행을 실시간 표시하도록 `text/event-stream` 엔드포인트 제공 가능.
- `run_workflow`의 `on_node_event` 콜백을 SSE 이벤트로 중계.

## 출력
완료 시 `_workspace/02_backend_status.md`에 구현 엔드포인트 목록·실제 요청/응답 shape·mcp-engineer에 요구한 진입 함수 시그니처를 기록한다 (frontend·qa가 참조).
