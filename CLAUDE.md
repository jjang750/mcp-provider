# CLAUDE.md

이 파일은 Claude Code가 **mcp-provider** 프로젝트에서 작업할 때 자동 로딩되는 지침입니다.

## 프로젝트 개요

**mcp-provider** — OpenAPI/Swagger 스펙(파일 업로드 또는 URL)을 분석해 각 API 오퍼레이션을 **MCP 노드**로 변환하고, 드래그앤드롭 캔버스에서 **엣지로 순차 실행 워크플로우**를 구성·실행하는 웹 기반 no-code MCP 워크플로우 빌더.

- **백엔드:** Python 3.11 호환, FastAPI, SQLite (단일 파일)
- **MCP:** 완성된 워크플로우를 **MCP 서버로 외부 MCP 클라이언트(Claude Desktop 등)에 노출**. 실행 엔진은 내부적으로 실제 HTTP API를 호출.
- **프론트:** Jinja2 서버 렌더링 + htmx 부분 갱신 + **Drawflow** 드래그앤드롭 비주얼 캔버스 (빌드 도구 없음, CDN/정적)
- **DB:** SQLite

## 하네스: MCP Provider 빌드/유지보수

**목표:** Swagger→MCP 워크플로우 빌더를 전문 에이전트 팀(architect / backend / mcp / frontend / qa)으로 구축·확장·유지보수한다.

**트리거:** 이 프로젝트의 기능 구현·확장·수정·디버깅·QA·재실행 요청 시 **`mcp-provider-builder`** 스킬을 사용하라. 단순 개념 질문이나 파일 위치 확인은 직접 응답 가능.

**실행 모드:** 하이브리드 — 설계(단독 architect) → 구현(에이전트 팀) → QA(단독 qa-integrator, 점진적).

**변경 이력:**
| 날짜 | 변경 내용 | 대상 | 사유 |
|------|----------|------|------|
| 2026-06-10 | 초기 하네스 구성 (에이전트 5, 스킬 7) | 전체 | 신규 구축 |
