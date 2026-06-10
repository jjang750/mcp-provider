---
name: openapi-to-mcp
description: OpenAPI/Swagger 스펙(2.0/3.0/3.1)을 파싱하여 각 API operation을 MCP 노드/도구 정의로 변환하는 방법. $ref 해석, 파라미터/인증 스킴/요청·응답 스키마 매핑을 다룬다. "스웨거 파싱", "OpenAPI 분석", "스펙에서 노드 생성", "operation 추출", "MCP 도구 매핑" 작업 시 반드시 사용. mcp-engineer 전용.
---

# OpenAPI → MCP 노드 생성

스펙을 분석해 각 operation을 노드(=MCP 도구 후보)로 변환한다. 출력은 `mcp-provider-architecture`가 정의한 `operations` 테이블 + 노드 스키마에 맞춘다.

## 왜 신중한 파싱이 필요한가
실세계 Swagger는 더럽다 — `$ref` 중첩, allOf/oneOf, 2.0과 3.x 구조 차이, 누락된 필드. 전체 실패보다 **부분 성공 + 경고 목록**이 사용자에게 유용하다. 한 operation 파싱이 실패해도 나머지는 살린다.

## 파싱 파이프라인
1. **로드 & 버전 감지** — `swagger: "2.0"` vs `openapi: "3.x"`. JSON/YAML 모두 지원.
2. **$ref 해석** — 내부 `#/components/...`(3.x) / `#/definitions/...`(2.0) 참조를 재귀 해석. 순환 참조는 1회 펼친 뒤 `{"$circular": true}`로 표시.
3. **operation 순회** — `paths.{path}.{method}` 마다 하나의 operation 추출.
4. **operationId 보장** — 없으면 `{method}_{path_slug}`로 생성.
5. **파라미터 분류** — path/query/header/cookie + requestBody. 2.0의 `in: body`는 3.x requestBody로 정규화.
6. **인증 매핑** — securitySchemes(apiKey/http bearer/oauth2)를 노드 실행 시 주입 가능한 형태로 기록.
7. **저장** — operations 테이블에 method/path/params_schema/request_schema/response_schema/auth(JSON) 기록.

## 권장 라이브러리 (Python 3.11)
- 파싱/검증: `prance` 또는 `openapi-core`, 단순하게는 `PyYAML`+수동 해석. `$ref` 해석은 `jsonref` 활용 가능.
- 선택은 architect 계약과 합의. 무거운 검증보다 관대한 파싱 우선.

## operation → 노드 매핑 규칙
```jsonc
// operations 행 → 노드 후보
{
  "operation_id": "getUserById",
  "method": "GET",
  "path": "/users/{userId}",
  "params_schema": {
    "path": [{"name": "userId", "type": "string", "required": true}],
    "query": [], "header": []
  },
  "request_schema": null,                 // body 있으면 JSON schema
  "response_schema": {"200": {/* ... */}},
  "auth": {"type": "bearer"}
}
```
노드 생성 시 `params_schema`를 프론트 파라미터 편집 패널이 폼으로 렌더링할 수 있도록 평탄한 구조로 제공한다 (frontend와의 경계).

## MCP 도구 변환 (노출 단계)
- 노드/워크플로우 → MCP 도구의 inputSchema(JSON Schema)로 변환.
- path/query/required body 필드 → inputSchema properties + required.
- 공식 `mcp` SDK의 도구 등록 형식에 맞춘다 (상세는 `mcp-workflow-engine` 스킬).

## 엣지 케이스 체크리스트
- [ ] 2.0 `consumes/produces` → content-type 처리
- [ ] `$ref` 순환 / 깊은 중첩
- [ ] allOf/oneOf/anyOf 병합 (allOf는 머지, oneOf/anyOf는 첫 후보 + 대안 기록)
- [ ] operationId 중복 → suffix로 유일화
- [ ] 서버 URL: 3.x `servers[]`, 2.0 `host`+`basePath`+`schemes` → base_url 결정
- [ ] 잘못된 스펙 → 파싱 가능한 부분만 저장 + `warnings[]` 반환

## 출력
`_workspace/03_engine_status.md`에 노드 생성 규칙, 지원 스펙 버전, 알려진 미지원 케이스를 기록한다.
