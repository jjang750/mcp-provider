# 핸드오프 — UI 리디자인 (디자인 핸드오프 적용)

대상: 디자인 번들 "MCP Provider — 워크플로우 빌더 UI 리디자인" 적용.
전체 3단계 계획 중 **1단계(비주얼) 완료**, 2(정렬 도구)·3(제어 흐름 노드) 예정.

## 1단계 — 비주얼 리디자인 (완료, 2026-06-16)

### 변경
| 파일 | 변경 |
|------|------|
| `static/tokens.css` (신규) | 라이트(기본)+다크 디자인 토큰. `:root` / `[data-theme="dark"]`. 핸드오프 값 그대로 |
| `templates/base.html` | 테마 부트스트랩 스크립트(FOUC 방지, localStorage `mcp-theme`), Google Fonts(Plus Jakarta Sans/JetBrains Mono), tokens.css 링크 |
| `static/style.css` | 토큰 기반 전면 리스타일 — 레이아웃(3분할 236/1fr/296), 버튼/인풋/배지/탭/노드카드/엣지/다이얼로그/실행로그/팔레트. 기존 클래스명 유지(JS 호환) |
| `templates/editor.html` | 툴바 2행(1행: 제목·저장상태·테마토글·저장·실행 / 2행: +start·+end·도구이름·그룹·MCP노출), 테마 토글 버튼 |
| `static/canvas.js` | `nodeHtml` 메서드 배지+경로 렌더, `toggleTheme`/`syncThemeIcon`, `applyNodeStatuses`(실행 후 노드 상태색), expose 버튼 라벨 |

### 핵심 동작
- **테마**: 우상단 🌗/🌙 버튼으로 라이트↔다크, localStorage 영속. 새 토큰은 두 테마 모두 정의됨.
- **노드 카드**: `METHOD /path` 라벨을 메서드 배지(색상별)+경로로 표시. 실행 후 성공(초록)/실패(빨강)/스킵(점선) 테두리 색 자동 반영(`applyNodeStatuses`, `node-{status}` 클래스).
- **메서드 배지**: 팔레트(`.method-*`)·노드(`.df-method`) 공통 색(GET 파랑/POST 그린/PUT 앰버/PATCH 바이올렛/DELETE 레드).
- 접근성: primary 버튼 채움은 AA 안전색 `--brand-solid`(#0A7D5A) 사용.

### 1단계 보강 — 노드 카드 목업 재현 (2026-06-16)
"색·폰트는 적용됐는데 목업과 다르다" 피드백 반영. 목업의 노드 카드를 정확히 재현:
- 상단 4px **상태바 스트라이프**(클립된 라운드), 메서드 배지 + 경로(mono) + 우측 상태표식.
- 실행 후 성공(✓·초록 스트라이프)/실패(!·빨강)/스킵(점선·회색)/실행중(그라데이션) 자동 반영.
- start/end = 아이콘 타일(▶ 브랜드 / ■ 중립) + 시작/종료 라벨.
- `nodeHtml` 구조 변경(stripe+body), `applyNodeStatuses`가 상태표식까지 주입, `style.css` 노드 블록 재작성.
- ⚠️ 아직 미반영(후속): 캔버스 줌 컨트롤, 정렬 플로팅 바(2단계), 실행 다이얼로그 세그먼트, 좌/우 패널 세부.

### 검증
- 순수 로직(`nodeHtml` 라벨 파싱→배지/스트라이프/타일, 상태 keyToDf 매핑) `/tmp` 단위 테스트 통과.
- CSS/HTML 시각 확인·git 커밋은 샌드박스 마운트 이슈로 미수행 → 로컬 브라우저에서 확인 필요(라이트/다크 토글, 노드 실행 색).

## 화면 직접 확인 후 수정 (2026-06-16, http://127.0.0.1:9000/editor/2)
브라우저로 실제 렌더 확인 후 목업과 다른 점 2건 수정·검증 완료:
- **노드 제목**: 경로 중복(`/impo/detail`) → 오퍼레이션 **요약**("세대별 관리비 부과 내역 조회"). 로드 시 `refreshNodeTitles()`가 `ensureOperation`으로 요약을 받아 `.wf-node__title` 갱신(요약 없으면 경로 폴백).
- **포트**: Drawflow 기본 노란 점 → 흰 원+회색 테두리(호버 브랜드색), 연결선 `--border-strong`. (`drawflow_node_styles.css` §7·§8)
- 결과: 노드 카드(스트라이프·배지·경로·요약·start/end 타일)·포트·연결선이 목업과 일치.

### 남은 차이(미해결, 다음 작업)
- **좌측 패널**: 목업은 "오퍼레이션 검색+리스트"만. 실제 앱은 상단에 스펙 업로드 폼이 큼 → 스펙 업로드 접이식 + 오퍼레이션 검색/리스트 정리 권장(기능 유지).
- 실행 다이얼로그 인증/파라미터 그룹 세부, 실행 로그 카드 상태 원형 아이콘 등 미세 디테일.

---

## 노드 스타일 교체 — drawflow_node_styles.css 적용 (2026-06-16)
사용자 제공 CSS로 노드 카드 스타일 전면 교체("빨강 노드 버그" 수정: 상태색을 카드 전체가 아닌 **상단 스트라이프+얇은 테두리**에만).
- 신규 `static/drawflow_node_styles.css`(`.wf-node` 체계, `data-status` 토글) + base.html 링크.
- `style.css`의 구 `.df-*` 노드 블록 제거(팔레트 `.method-*`만 유지), 다중선택 하이라이트도 `.wf-node`로 이전.
- `canvas.js` `nodeHtml` → `.wf-node` 마크업(메서드 배지/경로/제목 + start/end SVG 아이콘), `applyNodeStatuses` → `data-status`(failed→error 매핑)·상태표식.
- 검증: 마크업 분기·상태 매핑 `/tmp` 테스트 통과, 구 클래스 잔여 참조 없음.

## 2단계 — 캔버스 도구 (일부 완료, 2026-06-16)
완료:
- **줌 컨트롤**(좌하단 +/−/% , Drawflow `zoom_in/out/reset`, `zoom` 이벤트로 배율 표시).
- **자동 정렬**(상단 중앙 플로팅 바): 엣지 기반 longest-path 레이어링으로 좌→우 재배치. 외부 라이브러리 없이 `pos_x/pos_y` 재작성 + `updateConnectionNodes()` + 200ms 애니메이션. 정렬 후 저장 필요.
- 파일: `templates/editor.html`(canvas-wrap+플로팅바+줌), `static/style.css`(오버레이), `static/canvas.js`(`zoomIn/Out/Reset`,`updateZoomLabel`,`autoLayout`).
- 검증: 레이어링 로직 `/tmp` 단위 테스트 통과(선형·다이아몬드 longest-path·사이클 안전).

**맞춤/균등 분배 (완료, 2026-06-16)**:
- 다중선택: 캔버스 클릭 위임(`onCanvasClickSelect`), shift로 누적, `.multi-selected` 하이라이트(Drawflow 이벤트 순서 충돌 회피).
- 플로팅 바에 맞춤(좌/가로중앙/우/위/세로중앙/아래) + 균등분배(가로/세로) 아이콘 버튼 + 선택 카운트. 2개 미만이면 비활성.
- `alignNodes(mode)`/`distributeNodes(axis)` — `pos_x/pos_y` 재작성(`applyPos` 공통화) + 애니메이션. 분배는 3개 이상.
- 검증: 맞춤(좌/우/중앙)·분배(균등 간격) 계산 `/tmp` 단위 테스트 통과.

### 실행 다이얼로그 재구성 (완료, 2026-06-16)
목업대로 재구성:
- 헤더: 아이콘 타일(▶ brand-weak) + 제목 + 서브타이틀(연결 API 노드 수) + 닫기(✕).
- **모드 세그먼트**: `폼 모드` / `JSON 직접 편집`(체크박스→세그먼트 버튼, `setRunMode`).
- **인증 세그먼트**: `Bearer Token` / `API Key` + 단일 토큰 입력(masked). `setAuthType`로 선택, `collectRunForm`이 선택 타입에 따라 `auth.token`/`auth.api_key` 생성. (기존 token/api_key 2입력 → 통합)
- 푸터: 취소(ghost) / ▶ 실행하기(primary).
- 파일: `templates/editor.html`(다이얼로그 마크업), `static/style.css`(`.segment`/`.seg-btn`/`.run-icon`), `static/canvas.js`(`setRunMode`/`setRunModeUI`/`setAuthType`/`updateRunSubtitle`, `collectRunForm`·`runWorkflow` 리팩터, 상태변수 `runMode`/`runAuthType`).
- 검증: 인증 타입별 auth 생성·JSON↔폼 복원 `/tmp` 단위 테스트 통과, 잔여 참조(run-json-toggle 등) 없음 확인.

## 3단계 — 제어 흐름 노드 (예정, 대규모)
IF/Switch/Loop/Batch/Merge/Filter. 다중 출력 포트, 로직 노드 색, 조건 빌더, **엔진을 순차→DAG+루프**로 확장(executor 변경), 스킵 분기 표시, MCP 입력스키마 분기 통합 정책.

## 이슈/리스크
- 마운트 truncation으로 자동 통합검증 불가 → 단계마다 로컬 확인 권장.
- 3단계는 실행 모델 변경이라 별도 설계/플랜 필요.
