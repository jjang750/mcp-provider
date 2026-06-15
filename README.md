# mcp-provider

OpenAPI/Swagger 문서를 업로드하거나 URL로 가져오면, 각 API 오퍼레이션을 **MCP 노드**로 변환하고 드래그앤드롭 캔버스에서 **엣지로 연결해 순차 실행**하는 웹 기반 no-code **MCP 워크플로우 빌더**입니다. 완성된 워크플로우는 MCP 서버로 외부 MCP 클라이언트(Claude Desktop 등)에 노출할 수 있습니다.

## 주요 기능

- 📄 **스펙 입력** — Swagger/OpenAPI 2.0 / 3.0 / 3.1 문서를 파일 업로드 또는 URL로 가져와 파싱 (`$ref` 재귀 해석, 부분 실패 시 경고 누적)
- 🧩 **노드 자동 생성** — 스펙의 각 API 오퍼레이션을 노드(=MCP 도구 후보)로 추출 (method/path/파라미터/인증/응답 스키마)
- 🎨 **드래그앤드롭 캔버스** — [Drawflow](https://github.com/jerosoler/Drawflow) 기반 비주얼 에디터로 노드 배치·엣지 연결
- 🔗 **데이터 매핑 + 자동 채움** — 엣지에 `data_mapping`을 지정해 선행 노드 출력을 다음 노드 입력으로 전달. 선행 노드의 응답 필드를 칩으로 표시하고, 이름 매칭으로 매핑을 자동 제안 (필드명이 없으면 직접 입력)
- ▶️ **순차 실행** — 그래프를 위상정렬해 노드를 순서대로 실행하고 실제 HTTP API를 호출, 노드별 실행 로그 기록
- 🔌 **MCP 서버 노출** *(설계 완료, 1차 부분 구현)* — `mcp_exposed` 워크플로우를 MCP 도구로 노출

## 기술 스택

| 영역 | 기술 |
|------|------|
| 런타임 | Python **3.11** |
| 백엔드 | FastAPI, Uvicorn, Pydantic v2 |
| DB | SQLite (단일 파일 `mcp_provider.db`) |
| 파싱/실행 | PyYAML, httpx |
| 프론트엔드 | Jinja2 (서버 렌더링) + htmx (부분 갱신) + Drawflow (캔버스), 빌드 도구 없음 |
| MCP | 공식 `mcp` Python SDK |

## 프로젝트 구조

```
mcp-provider/
├── backend/                 # FastAPI 앱 + SQLite (영속화·HTTP API)
│   ├── app.py               #   앱·lifespan(DDL 마이그레이션)·페이지 라우트·정적/템플릿 마운트
│   ├── db.py                #   SQLite 연결, idempotent 스키마 생성
│   ├── models.py            #   Pydantic 모델 (Node/Edge/WorkflowGraph/ExecutionResult 정본)
│   ├── engine_bridge.py     #   engine 모듈 호출 래퍼
│   ├── mcp_server.py        #   MCP 노출 진입점
│   ├── repositories/        #   specs / workflows / executions CRUD (+ JSON 직렬화)
│   └── routers/             #   specs, workflows(+run), executions, operations
├── engine/                  # OpenAPI 파서 + 실행 엔진 (FastAPI 비의존 순수 모듈)
│   ├── parser.py            #   OpenAPI 2.0/3.x 파싱, $ref 해석
│   ├── executor.py          #   run_workflow: 위상정렬·data_mapping 주입·HTTP 호출·로그
│   └── http_client.py       #   httpx 래퍼 (URL 빌드·인증 주입·타임아웃)
├── templates/               # Jinja2 + htmx (index, editor, partials)
├── static/                  # canvas.js (Drawflow ↔ 그래프 직렬화), style.css
├── requirements.txt
└── mcp_provider.db          # 런타임 생성 (커밋 금지)
```

## 설치 및 실행

```powershell
# Windows PowerShell 기준
cd ~\workspace\mcp-provider

# 가상환경
python -m venv venv
.\venv\Scripts\Activate.ps1

# 의존성 설치
pip install -r requirements.txt

# 서버 실행 (8000이 사용 중이면 다른 포트 지정)
uvicorn backend.app:app --reload --port 9000
```

```bash
# macOS / Linux
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn backend.app:app --reload --port 9000
```

실행 후 브라우저에서:

- `http://localhost:9000/` — 워크플로우 목록
- `http://localhost:9000/editor/{id}` — 캔버스 에디터

> 백엔드와 프론트엔드는 **단일 FastAPI 서버**로 함께 제공됩니다(Jinja2 렌더 + StaticFiles). 별도 프론트 서버가 필요 없습니다.

## 사용법 (Petstore 예시)

1. **스펙 업로드** — 에디터 좌측 패널에서 Swagger 파일을 업로드하거나 URL 입력
   (예: `https://petstore3.swagger.io/api/v3/openapi.json`)
2. **노드 배치** — 추출된 오퍼레이션 팔레트에서 GET·POST 노드를 캔버스로 드래그
3. **엣지 연결** — 두 노드를 연결하면 응답 필드 ↔ 입력 파라미터가 이름 매칭으로 **자동 매핑** 제안됨
4. **매핑 조정** — 엣지를 선택해 응답 필드 칩(`$.id` 등)·입력 파라미터 칩(`params.path.petId` 등)을 클릭 삽입하거나 직접 수정
5. **저장 → 실행** — 그래프를 저장(PUT)하고 실행(Run). 하단 로그 영역에서 노드별 입력/출력/상태 확인

## REST API 요약

베이스 prefix `/api`.

| Method | Path | 설명 |
|--------|------|------|
| POST | `/api/specs/upload` | 파일 업로드 → 파싱 → 오퍼레이션 저장 (multipart, field `file`) |
| POST | `/api/specs/from-url` | URL fetch → 파싱 → 저장 |
| GET | `/api/specs` | 스펙 목록 |
| GET | `/api/specs/{spec_id}/operations` | 스펙의 오퍼레이션(노드 후보) 목록 |
| GET | `/api/operations/{operation_id}` | 오퍼레이션 단건 메타(응답/입력 스키마) 조회 |
| GET / POST | `/api/workflows` | 워크플로우 목록 / 생성 |
| GET / PUT / DELETE | `/api/workflows/{id}` | 단건 조회 / 그래프 저장 / 삭제 |
| POST | `/api/workflows/{id}/run` | 순차 실행(동기) → `ExecutionResult` 반환 |
| GET | `/api/executions/{exec_id}` | 실행 상태 + 로그 |
| GET | `/api/health` | 헬스 체크 |

### 그래프 JSON shape (저장/실행 단위)

```jsonc
{
  "nodes": [{
    "id": "node_1", "type": "api_call", "label": "GET /pet/{petId}",
    "operation_id": 42,
    "params": { "path": {"petId": 10}, "query": {}, "header": {}, "body": null },
    "position": { "x": 120, "y": 80 }
  }],
  "edges": [{
    "id": "edge_1", "source": "node_0", "target": "node_1",
    "data_mapping": [{ "from": "$.id", "to": "params.path.petId" }]
  }]
}
```

- `data_mapping`의 `from`은 선행 노드 출력 경로(JSONPath 부분집합: `$`, dotted, `[i]`), `to`는 후행 노드 입력 경로(`params.path.x` / `params.query.x` / `params.header.x` / `params.body[.x]`).
- 동적 값은 전부 엣지 `data_mapping`으로 주입되며, 노드 `params`는 정적 기본값만 담습니다.

## 동작 메모

- **노드 실패 처리** — 실행 중 노드가 실패해도 HTTP는 `200`을 반환하고 `status: "failed"` + 로그로 보고합니다(부분 결과·로그 유실 방지). 클라이언트는 `ExecutionResult.status`로 판정합니다.
- **인증** — 시크릿은 DB에 영구 저장하지 않고 실행 시점(`/run`의 `auth` 필드)에 주입합니다.
- **순차 실행만** — 1차 버전은 위상정렬 후 직렬 실행합니다(병렬 분기 미지원).

## 현재 범위 / 로드맵

**구현 완료**: 스펙 업로드(파일/URL) · 오퍼레이션 추출 · 캔버스 그래프 저장/로드 · 데이터 매핑(자동 채움 포함) · 순차 실행 · 실행 로그

**미구현 / 예정**:
- 실시간 실행 로그 스트리밍 (SSE)
- MCP 서버 실제 노출 (`/api/workflows/{id}/expose` 와이어링)
- 분기·병렬 실행
- `transform` 노드 변환 로직 (현재 패스스루)

## 라이선스

내부 프로젝트 (라이선스 미지정).
