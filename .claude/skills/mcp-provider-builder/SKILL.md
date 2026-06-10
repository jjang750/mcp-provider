---
name: mcp-provider-builder
description: mcp-provider(Swagger→MCP 워크플로우 빌더) 프로젝트를 전문 에이전트 팀으로 구축·확장·수정·디버깅·QA하는 오케스트레이터. OpenAPI 파싱, MCP 노드/엣지 생성, 순차 실행 엔진, MCP 서버 노출, FastAPI+SQLite 백엔드, htmx/Jinja2/Drawflow 캔버스 UI 작업을 조율한다. 다음 요청 시 반드시 사용: "mcp-provider 구현/구축", "스웨거 업로드 기능", "노드/엣지/워크플로우 기능", "실행 엔진", "MCP 서버 노출", "백엔드/프론트 만들어", "이 프로젝트 기능 추가/수정/확장/보완", "다시 실행/재실행/업데이트", "QA/통합 검증", "버그 수정", "이전 결과 기반으로 개선". 단순 개념 질문·파일 위치 확인은 직접 응답 가능.
---

# MCP Provider Builder — 오케스트레이터

Swagger→MCP 워크플로우 빌더를 **하이브리드 모드**로 구축한다. 설계는 단독 architect, 구현은 에이전트 팀(backend/mcp/frontend), QA는 단독 qa-integrator로 점진 검증한다.

## 에이전트·스킬 구성
| 에이전트 | 타입 | 스킬 | 책임 |
|----------|------|------|------|
| architect | general-purpose | mcp-provider-architecture | 공유 계약·스키마 설계 |
| backend-engineer | general-purpose | fastapi-sqlite-backend | FastAPI+SQLite, 업로드, CRUD |
| mcp-engineer | general-purpose | openapi-to-mcp, mcp-workflow-engine | 파싱·노드생성·실행엔진·MCP노출 |
| frontend-engineer | general-purpose | htmx-canvas-ui | Jinja2+htmx+Drawflow 캔버스 |
| qa-integrator | general-purpose | integration-qa | 경계면 교차 검증 |

> 모든 Agent 호출에 `model: "opus"` 명시. 에이전트 정의는 `.claude/agents/`, 스킬은 `.claude/skills/`.

## Phase 0: 컨텍스트 확인 (필수, 먼저 실행)
1. `_workspace/` 존재 여부 확인.
2. 실행 모드 판별:
   - **`_workspace/` 미존재** → **초기 실행** (Phase 1부터 전체)
   - **`_workspace/` 존재 + 사용자가 부분 수정 요청** → **부분 재실행** (해당 에이전트만 재호출, 관련 status 파일 갱신)
   - **`_workspace/` 존재 + 새 입력/전면 재작업** → **새 실행** (기존 `_workspace/`를 `_workspace_prev/`로 이동 후 초기 실행)
3. 기존 소스(`backend/`, `frontend/`, `templates/`)가 있으면 각 에이전트는 전면 재작성 대신 변경분만 수정 (에이전트 재호출 지침).

## Phase 1: 설계 (단독 — architect)
**실행 모드:** 서브 에이전트 (단독)
- `Agent(subagent_type: general-purpose, model: "opus")`로 architect 역할 호출. 에이전트 정의 `.claude/agents/architect.md`와 `mcp-provider-architecture` 스킬을 따르도록 지시.
- 입력: 사용자 요구 + (있으면) 샘플 Swagger.
- 출력: `_workspace/01_architect_contracts.md` (SQLite 스키마, 노드/엣지 JSON, REST 계약, MCP 매핑, 모듈 소유권).
- **게이트:** 계약 문서가 architect 산출 체크리스트를 만족하는지 확인 후 다음 Phase.

## Phase 2: 구현 (에이전트 팀)
**실행 모드:** 에이전트 팀
1. `TeamCreate`로 팀 구성: backend-engineer, mcp-engineer, frontend-engineer.
2. `TaskCreate`로 작업 할당 (의존성 명시):
   - mcp-engineer: 파서+노드생성 → 실행엔진 → MCP노출 (`run_workflow` 시그니처를 가장 먼저 확정해 공유)
   - backend-engineer: 계약 기반 앱/DB/엔드포인트, mcp-engine 모듈 import 호출
   - frontend-engineer: backend·engine status 확정 후 캔버스/템플릿
3. 팀원은 `SendMessage`로 자체 조율 — 특히 **shape 변경 시 즉시 통지**(API shape, 노드/엣지 스키마, run_workflow 시그니처).
4. 각자 완료 시 `_workspace/0X_*_status.md` 산출.
5. 리더(오케스트레이터)는 진행 모니터링, 교착 시 중재, status 파일 수집.

**핵심 의존 순서:** architect 계약 → (mcp-engine `run_workflow` 시그니처 + backend API shape 확정) → frontend는 backend/engine status 확정 후 시작. 이 순서를 SendMessage로 강제.

## Phase 3: 통합 QA (단독 — qa-integrator, 점진적)
**실행 모드:** 서브 에이전트 (단독, incremental)
- 각 모듈 status가 나올 때마다 해당 경계를 검증 (전체 완성 후 1회 아님).
- `Agent(subagent_type: general-purpose, model: "opus")`로 qa-integrator 호출, `integration-qa` 스킬 따름.
- 출력: `_workspace/05_qa_report.md`.
- 버그 발견 시: 해당 모듈 담당 에이전트를 **부분 재호출**하여 수정 → 재검증 루프. critical 버그는 사용자 보고 전 반드시 수정.

## 데이터 전달 프로토콜
- **태스크 기반** (`TaskCreate`/`TaskUpdate`): 진행상황·의존 관리 (팀 모드)
- **파일 기반** (`_workspace/0X_*.md`): 계약·status 산출물, 감사 추적
- **메시지 기반** (`SendMessage`): shape 변경 실시간 통지
- 파일명 컨벤션: `01_architect_contracts.md`, `02_backend_status.md`, `03_engine_status.md`, `04_frontend_status.md`, `05_qa_report.md`
- 최종 소스는 `backend/`·`frontend/`(또는 backend의 templates/static)에, 중간 산출물(`_workspace/`)은 보존.

## 에러 핸들링
- 에이전트 1회 재시도 후 재실패 시: 해당 결과 없이 진행하되 최종 보고에 **누락 명시**.
- 상충 데이터(계약 vs 구현 불일치)는 삭제하지 않고 **출처 병기**, qa-integrator가 판정.
- 팀 교착(서로의 status 대기)이면 오케스트레이터가 architect 계약을 근거로 한쪽을 확정시켜 푼다.
- 환경/의존성 문제는 코드 결함과 구분해 보고.

## 팀 정리
- 구현 Phase 종료 후 `TeamDelete`. QA는 단독 서브로 별도 실행.
- Phase 간 팀 재구성 가능 — 산출물은 항상 `_workspace/` 파일로 보존된 상태.

## 완료 후 (Phase 7 진화 훅)
실행 완료 시 사용자에게 피드백 기회를 제공한다: "결과에서 개선할 부분이나, 팀 구성·워크플로우에 바꾸고 싶은 점이 있나요?" 피드백이 오면 유형별로 반영하고 `CLAUDE.md` 변경 이력에 기록:
- 결과 품질 → 해당 스킬 수정 / 에이전트 역할 → 에이전트 .md / 워크플로우 순서 → 이 오케스트레이터 / 트리거 누락 → description 확장.

## 테스트 시나리오
**정상 흐름:** "Petstore swagger.json 업로드해서 GET/POST 노드 두 개를 순서대로 실행하는 워크플로우 만들어" → Phase0(초기실행 판별) → architect 계약 → 팀 구현(파서로 operations 추출, 캔버스에서 노드 배치/엣지 연결, 저장, run_workflow 실행) → qa 경계 검증 → 동작하는 앱 + qa_report.

**에러 흐름:** "프론트에서 워크플로우 저장하면 실행 시 노드 입력이 비어" → Phase0(부분 재실행 판별) → qa-integrator가 경계2(캔버스 JSON ↔ 엔진 노드 스키마) 검증 → `data_mapping` 키명 불일치 발견 → frontend-engineer 또는 mcp-engineer 부분 재호출로 수정 → 재검증 → 보고.
