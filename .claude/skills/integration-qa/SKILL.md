---
name: integration-qa
description: mcp-provider 모듈 간 경계면(API↔프론트↔엔진↔MCP) 통합 정합성 검증 방법론. shape 불일치·dead link·계약 위반을 실제 실행으로 검출한다. "통합 QA", "경계면 검증", "정합성 확인", "통합 테스트", "shape 비교", "QA 리포트" 작업 시 반드시 사용. qa-integrator 전용. 각 모듈 완성 직후 점진적으로 실행한다.
---

# 통합 QA — 경계면 교차 검증

"파일이 존재하는가"가 아니라 **"두 모듈의 데이터 계약이 실제로 일치하는가"**를 검증한다. 경계 양쪽 코드를 동시에 읽고 비교하는 것이 핵심.

## 왜 경계면인가
각 모듈은 독립적으로는 정상이어도, 한쪽이 보내는 shape과 다른 쪽이 기대하는 shape이 다르면 런타임에 조용히 깨진다. 단위 테스트는 이를 못 잡는다 — 경계 교차 비교만이 잡는다.

## 점진적 실행 (incremental)
전체 완성 후 1회가 아니라, 모듈이 `_workspace/0X_*_status.md`를 낼 때마다 그 경계를 즉시 검증한다:
- backend status 나오면 → API 계약 vs architect 계약 비교
- frontend status 나오면 → 캔버스 JSON vs 엔진 노드 스키마 비교
- engine status 나오면 → run_workflow 시그니처 vs backend 호출부 비교

## 4대 검증 경계

### 1. backend API ↔ frontend
- 프론트의 모든 `hx-*`/fetch 호출 URL·메서드를 추출 → backend 라우터에 실제 존재하는가
- 응답 필드명·타입이 프론트가 읽는 필드와 일치하는가
- 검증: `02_backend_status.md`의 응답 shape과 `04_frontend_status.md`의 사용 필드 대조

### 2. frontend 캔버스 JSON ↔ engine 노드/엣지 스키마
- `canvas.js`의 `toContractGraph` 출력 shape vs 엔진 파서가 읽는 shape
- node: id/type/operation_id/params/position 키 일치
- edge: source/target/data_mapping 구조 일치
- **가장 자주 깨지는 경계** — 키 이름 하나만 달라도 실행 실패

### 3. backend ↔ mcp-engine
- backend가 호출하는 `run_workflow(...)` 인자/반환 vs 엔진 실제 시그니처
- 파서 호출부: 반환 `warnings[]`·operations shape 일치

### 4. MCP 노출 ↔ 워크플로우 모델
- MCP 도구 inputSchema vs start 노드 요구 입력
- 도구 핸들러가 `run_workflow`를 올바른 initial_input으로 호출하는가

## 실제 실행 검증 (general-purpose이므로 가능)
- **import 스모크:** `python -c "import backend.app"` 류로 모듈 로드 확인
- **스키마 일치 스크립트:** 두 status 파일/소스에서 키 집합 추출 후 diff
- **엔드포인트 호출:** 가능하면 `pytest`+`httpx.AsyncClient` 또는 앱 기동 후 `curl`로 핵심 플로우(업로드→노드생성→저장→실행) 1회 통과
- 환경 문제로 실패하면 환경 원인과 코드 원인을 **구분**해 보고

## 경계면 버그 패턴 체크리스트
- [ ] 응답 필드명 불일치 (camelCase vs snake_case)
- [ ] 프론트가 호출하는 엔드포인트가 backend에 없음 (dead link)
- [ ] 캔버스 노드 키와 엔진 기대 키 불일치
- [ ] data_mapping 구조 불일치 (배열 vs 객체, from/to 키명)
- [ ] Pydantic 모델 필수 필드를 프론트가 안 보냄
- [ ] 실행 status 값 집합 불일치 (프론트 표시 vs backend 저장)
- [ ] Python 3.11 비호환 문법 사용 (3.12+ 문법 혼입)

## 출력
`_workspace/05_qa_report.md`:
- 경계별 PASS/FAIL
- 발견 버그: 심각도(critical/major/minor) + 정확한 `파일:라인` + 경계 양쪽 증거 + 수정 제안
- 재검증 필요 항목
버그는 해당 모듈 담당에게 SendMessage로 직접 수정 요청하거나 오케스트레이터에 보고한다.
