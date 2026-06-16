# 핸드오프 — 노드별 Base URL 오버라이드 (2026-06-16)

## 이번 작업
실행 오류 `GET /impo/detail failed: Request URL is missing an 'http://' or 'https://' protocol.` 해결.
근본 원인: 업로드된 OpenAPI 스펙에 서버 주소(servers/host)가 없어 `operations.base_url`이 전부 NULL → 실행 시 경로만으로 호출되어 httpx가 거부.

## 설계
노드별 `base_url` 오버라이드 도입. 실행 시 우선순위:
`노드.base_url → 오퍼레이션.base_url → 기본값(MCP_DEFAULT_BASE_URL, 기본 http://localhost:8000)`.
- 노드마다 서로 다른 API 호스트 호출 가능 (요구사항).
- 미입력 시 기본값으로 동작 → 현재 오류 자동 해소.

## 변경 파일
| 파일 | 변경 |
|------|------|
| `backend/models.py` | `Node`에 `base_url: Optional[str] = None` 추가 |
| `backend/db.py` | `nodes.base_url TEXT` 컬럼 + 멱등 컬럼 마이그레이션(`_apply_column_migrations`, `init_db`에서 호출) |
| `backend/repositories/workflows.py` | `_load_nodes`에서 base_url 로드, `replace_graph`에서 base_url 저장 |
| `engine/executor.py` | `DEFAULT_BASE_URL` 상수 + `_execute_api_call`에서 effective base_url 우선순위 적용 |
| `engine/http_client.py` | URL에 http(s):// 없으면 명확한 한글 에러로 즉시 실패(재발 방지) |
| `static/canvas.js` | 속성 패널(api_call) 상단 **Base URL 입력란**, `DEFAULT_BASE_URL` 상수, `to/fromContractGraph` 직렬화, `applyParamChange` 처리, `addApiNode` 기본값 |

## 검증 상태
- 핵심 로직(우선순위·build_url·프로토콜 가드)·DB 마이그레이션(추가/멱등/라운드트립): 샌드박스 `/tmp`에서 단위 검증 **통과**.
- ⚠️ 백엔드 통합 실행/`git` 커밋: **미수행**. 샌드박스의 프로젝트 폴더 Linux 마운트가 읽기 손상(truncate/null byte) 및 쓰기 불가 상태여서 bash/git 사용 불가. 코드 편집은 파일 도구로 정상 반영됨.

## 다음 단계 (사용자 로컬에서)
1. 백엔드 재시작 → `init_db()`가 `nodes.base_url` 컬럼을 자동 추가(기존 DB 유지).
2. 워크플로우 재실행. base_url 미입력이어도 `http://localhost:8000` 기본값으로 동작.
3. 다른 호스트가 필요한 노드는 속성 패널의 **Base URL** 칸에 입력 후 저장.
4. (선택) 기본값 변경: 백엔드 기동 시 환경변수 `MCP_DEFAULT_BASE_URL` 설정. 프론트 표시는 `static/canvas.js`의 `DEFAULT_BASE_URL` 상수와 일치시킬 것.
5. 버전 관리(로컬에서):
   ```bash
   git checkout -b feat/per-node-base-url
   git add backend/models.py backend/db.py backend/repositories/workflows.py \
           engine/executor.py engine/http_client.py static/canvas.js
   git commit -m "feat: 노드별 base_url 오버라이드 + 기본값/마이그레이션 (실행 URL 프로토콜 누락 오류 수정)"
   git push -u origin feat/per-node-base-url
   ```
6. 커밋 전 `python -m py_compile`로 6개 파일 문법 확인 권장.

## 이슈/리스크
- 샌드박스 마운트 손상으로 통합 테스트를 자동 수행하지 못함 → 로컬 재실행으로 확인 필요.
- 기본값 상수가 서버(executor)·프론트(canvas.js) 2곳에 존재 → 변경 시 동기화 필요.
- `_synctest.txt`(마운트 진단용 임시 파일)가 남아 있으면 삭제.

---

# 추가 작업 — 실행 입력 폼 전환 (2026-06-16)

## 배경
실행할 때마다 `initial_input`/`auth`를 raw JSON으로 직접 입력해야 해 불편 → 폼 입력으로 전환.

## 변경
| 파일 | 변경 |
|------|------|
| `templates/editor.html` | 실행 다이얼로그를 폼 모드(기본)+JSON 모드로 재구성. "JSON 직접 편집" 토글 추가 |
| `static/canvas.js` | `buildRunForm`/`collectStartInputKeys`/`topKeyOf`/`coerceVal`/`collectRunForm`/`toggleRunJsonMode` 추가, `openRunDialog`·`runWorkflow` 폼 대응, `runFormCache`(세션 값 보존) |
| `static/style.css` | `.run-head`, `.run-mode-toggle` 레이아웃 |

## 동작 (API 파라미터 기반 — 2026-06-16 개정)
- 처음에는 시작-엣지 매핑(`from`)에서 키를 뽑았으나, 시작 노드는 응답 스키마가 없어 `from` 매핑이 없으면 폼이 비는 문제가 있었음.
- 개정: 다이얼로그 오픈 시 **시작 노드에 직접 연결된 api_call 노드**(`startConnectedApiNodeIds`)의 `params_schema`(path/query/header)+requestBody를 `paramInputPaths`로 분석해 **노드별 파라미터 입력칸** 자동 생성. 매핑 불필요.
- 현재 노드 정적 params 값으로 prefill. 실행 시 입력값을 해당 노드의 정적 params 에 기록(`applyRunFormToNodes`→`setNodeParamByPath`)하고 `saveWorkflow()`로 영속 → 속성 패널과 동일 데이터.
- 값은 숫자/불리언/JSON 자동 해석(`coerceVal`), 안 되면 문자열. 빈 칸은 해당 파라미터 삭제.
- 인증은 `token`/`api_key` 입력칸(세션 보존). initial_input 은 폼 모드에서 `{}`.
- 고급/매핑 기반 흐름은 상단 "JSON 직접 편집" 토글로 기존 raw JSON 입력 사용.
- 연결된 API 노드가 없으면 안내 + JSON 폴백.
- 주의: 폼 입력은 노드 정적 params 를 덮어쓰며 저장됨(테스트 값이 그대로 워크플로우에 남음).

## 검증
- 순수 로직 단위 테스트 `/tmp`에서 **통과**: `coerceVal`, `startConnectedApiNodeIds`(시작 직결 api 노드만 선별), `get/setNodeParamByPath`(path/query/body/whole-body round-trip, 숫자/불리언 변환, 빈 값 삭제).
- canvas.js 전체 문법 체크·통합 실행·git 커밋은 샌드박스 마운트 truncation으로 미수행 → 로컬에서 브라우저 실행으로 확인 필요.

---

# 추가 작업 — MCP 서버 배선 + 노출 토글 (2026-06-16)

## 배경
Claude Desktop 접속은 됐으나 ① `python -m backend.mcp_server` 가 `No module named 'backend'` 로 죽고 ② 서버가 스텁이라 도구 0개/실행 실패. "입력 후 조회 안 됨"의 원인.

## 변경
| 파일 | 변경 |
|------|------|
| `backend/mcp_server.py` | `load_exposed_workflows()`(mcp_exposed 워크플로우 로드)·`make_operation_resolver()`(operations 조회) 실제 배선. `apply_tool_args()` 추가 — 도구 인자를 대상 노드 params(path/query/header/body)에 주입(deepcopy). `_call_tool`에서 호출 |
| `backend/routers/workflows.py` | `PUT /api/workflows/{id}/expose` 추가(`mcp_exposed` 토글), `ExposeRequest` 모델 |
| `templates/editor.html` | 툴바에 "MCP 노출: ON/OFF" 버튼 |
| `static/canvas.js` | `toggleExpose()`/`setExposeBtn()`, 로드 시 상태 반영 |

## 접속 (config) — import 오류 해결
`%APPDATA%\Claude\claude_desktop_config.json` 에 `env.PYTHONPATH` = 프로젝트 루트 추가(또는 루트 런처 스크립트 절대경로 실행). 그래야 `backend`/`engine` import.

## 사용 절차 (조회 되게 하기)
1. 에디터에서 워크플로우 열고 **"MCP 노출: ON"** 클릭 → 저장.
2. **Claude Desktop 재시작** (MCP 서버는 기동 시 1회만 도구 목록 생성 — 노출/그래프 변경 시 재시작 필수).
3. 도구 `workflow_{id}_{slug}` 가 뜸. 호출 방식 두 가지:
   - 필수 파라미터가 **정적 params**(실행 폼/속성에서 입력)면 inputSchema 비어 인자 없이 실행 → 결과 반환.
   - 정적이 아니면 inputSchema에 인자로 노출되고, 전달 시 `apply_tool_args`가 대상 노드 params로 주입 → 실행.
4. base_url 미설정이어도 기본값 `http://localhost:8000` 사용. 백엔드가 떠 있어야 함.

## 검증
- `/tmp` 인메모리 DB/단위 테스트 **통과**: 노출 워크플로우만 로드, 리졸버 동작, `build_input_schema`(정적 충족 시 빈 스키마), `apply_tool_args`(path/query/body 라우팅·deepcopy 비파괴).
- 실제 MCP 핸드셰이크/실행·git 커밋은 마운트 이슈로 로컬 확인 필요.

## 이슈/리스크
- MCP 도구 목록은 **서버 기동 시 고정** → 워크플로우 추가/노출/수정 후 Claude Desktop 재시작 필요(향후 동적 reload 고려).
- 도구 인자는 노드 정적 params 로 주입되어 그 실행에만 적용(원본 그래프는 deepcopy로 보존, DB 미변경).
- MCP 실행 auth는 현재 `{}` 고정(서버측 인증 주입 TODO).

---

# 추가 작업 — 도메인별 MCP 서버 분리 (2026-06-16)

## 배경
도구 이름을 `workflow_2_xperp` 식으로 도메인별로 나누고, MCP 서버 자체를 `xperp`/`xpvote` 로 분리하고 싶다는 요청.
- 참고: `slugify()`가 [a-z0-9]만 남기므로 한글 전용 이름은 fallback `workflow`가 됨. 이름에 영문 토큰 포함 시 슬러그에 반영.

## 설계
워크플로우에 `mcp_group` 태그를 달고, MCP 서버 프로세스를 `MCP_GROUP` 환경변수로 그룹별 필터링. config에 그룹별 커넥터를 여러 개 등록 → 서버가 갈림(서버명 `mcp-{group}`).

## 변경
| 파일 | 변경 |
|------|------|
| `backend/db.py` | `workflows.mcp_group TEXT` 컬럼 + 멱등 마이그레이션 |
| `backend/models.py` | `WorkflowSummary`/`WorkflowDetail` 에 `mcp_group` |
| `backend/repositories/workflows.py` | summary/detail 에 `mcp_group` 로드, `set_mcp_group()` 추가 |
| `backend/routers/workflows.py` | `PUT /expose` 에 `group` 필드(노출+그룹 동시 설정) |
| `backend/mcp_server.py` | `MCP_GROUP` 로 `load_exposed_workflows` 필터, `MCP_SERVER_NAME`(`mcp-{group}`) |
| `templates/editor.html` | 그룹 입력칸(onchange 자동 저장) |
| `static/canvas.js` | `applyMcpGroup()`/`toggleExpose()`(group 포함)·로드 시 복원 |

## 사용 절차
1. 에디터에서 워크플로우 열고 **그룹 입력칸에 `xperp`** 입력(자동 저장) → **MCP 노출: ON**.
2. 다른 워크플로우는 그룹 `xpvote` 등으로 지정.
3. `claude_desktop_config.json` 에 그룹별 커넥터 등록:
```json
{
  "mcpServers": {
    "xperp": {
      "command": "C:\\Users\\PC-727\\workspace\\mcp-provider\\venv\\Scripts\\python.exe",
      "args": ["-m", "backend.mcp_server"],
      "cwd": "C:\\Users\\PC-727\\workspace\\mcp-provider",
      "env": { "PYTHONPATH": "C:\\Users\\PC-727\\workspace\\mcp-provider", "MCP_GROUP": "xperp" }
    },
    "xpvote": {
      "command": "C:\\Users\\PC-727\\workspace\\mcp-provider\\venv\\Scripts\\python.exe",
      "args": ["-m", "backend.mcp_server"],
      "cwd": "C:\\Users\\PC-727\\workspace\\mcp-provider",
      "env": { "PYTHONPATH": "C:\\Users\\PC-727\\workspace\\mcp-provider", "MCP_GROUP": "xpvote" }
    }
  }
}
```
4. Claude Desktop 재시작 → `xperp` 서버엔 그룹 xperp 워크플로우만, `xpvote` 서버엔 xpvote만 도구로 노출.
   - `MCP_GROUP` 미지정 커넥터는 전체 노출(기존 단일 서버 호환).

## 검증
- `/tmp` 인메모리 단위 테스트 **통과**: 그룹 필터(xperp→wf2만, xpvote→wf3만, 무필터→노출 전체·비노출 제외), `set_mcp_group` blank→NULL 정규화, 서버명 `mcp-{group}` 도출.
- 실제 다중 커넥터 기동·git 커밋은 마운트 이슈로 로컬 확인 필요.

---

# 추가 작업 — 도구 이름 직접 지정 (2026-06-16)

## 배경
서버 분리보다 핵심 요구는 "도구 이름을 직접 지정해 이름만 봐도 어떤 앱인지 알게" 하는 것.

## 설계
워크플로우에 `mcp_tool_name` 추가. 값이 있으면 그걸 도구 이름으로 그대로 사용(MCP 안전문자 `[A-Za-z0-9_-]`로 정리), 없으면 기존 `workflow_{id}_{slug}`. 한글 가독성은 도구 **description**(=워크플로우 이름)에 그대로 노출.

## 변경
| 파일 | 변경 |
|------|------|
| `backend/db.py` | `workflows.mcp_tool_name TEXT` 컬럼 + 마이그레이션 |
| `backend/models.py` | `WorkflowSummary`/`Detail` 에 `mcp_tool_name` |
| `backend/repositories/workflows.py` | 로드 + `set_mcp_tool_name()` |
| `backend/routers/workflows.py` | `PUT /expose` 에 `tool_name` |
| `backend/mcp_server.py` | `sanitize_tool_name()`, `build_tool_name(override=)`, `build_tools`/`load_exposed_workflows`에 tool_name 전달 |
| `templates/editor.html` | "도구 이름" 입력칸(onchange 자동 저장) |
| `static/canvas.js` | `applyToolName()` + 로드 복원 |

## 사용
- 에디터 툴바 **"도구 이름"** 칸에 `xperp_charge_detail` 같은 영문 이름 입력(자동 저장) → 그게 곧 MCP 도구 이름.
- 비우면 자동(`workflow_{id}_{slug}`).
- **주의**: 도구 이름은 영문/숫자/`_`/`-` 만 유효(MCP 호환). 한글 입력 시 정리 후 비면 자동 이름으로 폴백 → 한글 설명은 description에 표시되므로 Claude 목록에서 같이 보임.
- 변경 후 Claude Desktop 재시작.

## 검증
- `/tmp` 단위 테스트 **통과**: override 우선·정리(공백/기호→`_`), 한글 override→폴백, 무 override→auto(`workflow_{id}_slug`), 세터 blank→NULL.
