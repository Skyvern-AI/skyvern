# Skyvern 코드베이스 분석 보고서

**분석 기준**: 커밋 `5b530ca` (2025-11-28)
**리포지토리**: https://github.com/Skyvern-AI/skyvern
**분석 목적**: 네이버 블로그 자동화 프로젝트를 위한 핵심 코드 위치 파악

---

## 1. 전체 아키텍처 개요

### 1.1 디렉토리 구조

```
skyvern/
├── cli/                    # CLI 명령어 (skyvern run, skyvern init 등)
├── config.py               # 전체 설정 관리 (LLM, 브라우저, 프록시 등)
├── forge/                  # 핵심 비즈니스 로직
│   ├── agent.py            # ForgeAgent - 메인 에이전트 클래스
│   ├── agent_functions.py  # 에이전트 함수들
│   ├── api_app.py          # FastAPI 서버
│   ├── prompts.py          # 프롬프트 템플릿 엔진
│   └── sdk/
│       ├── api/llm/        # LLM Provider 추상화 레이어
│       │   ├── api_handler_factory.py  # LLM 핸들러 팩토리
│       │   ├── config_registry.py      # LLM 설정 레지스트리
│       │   └── models.py               # LLM 모델 정의
│       ├── workflow/       # 워크플로우 엔진
│       │   ├── models/block.py         # Block 시스템 (Navigation, Action 등)
│       │   ├── service.py              # 워크플로우 서비스
│       │   └── context_manager.py      # 컨텍스트 관리
│       └── routes/         # API 라우트 정의
├── webeye/                 # 브라우저 자동화 엔진
│   ├── browser_factory.py  # 브라우저 생성/관리
│   ├── scraper/
│   │   └── domUtils.js     # DOM 스크래핑 + Bounding Box 그리기
│   ├── actions/
│   │   ├── handler.py      # 액션 핸들러 (클릭, 입력 등)
│   │   └── actions.py      # 액션 타입 정의
│   └── utils/
│       ├── page.py         # 페이지 유틸리티
│       └── dom.py          # DOM 유틸리티
└── skyvern-frontend/       # React 기반 웹 UI
```

### 1.2 핵심 의존성

| 패키지 | 버전 | 역할 |
|--------|------|------|
| `playwright` | - | 브라우저 자동화 (Chromium) |
| `litellm` | >=1.75.8 | LLM 추상화 레이어 (다중 Provider 지원) |
| `openai` | >=1.68.2 | OpenAI API 클라이언트 |
| `anthropic` | >=0.50.0 | Anthropic API 클라이언트 |
| `pillow` | >=10.1.0 | 이미지 처리 |
| `fastapi` | >=0.115.6 | REST API 서버 |
| `pydantic` | >=2.10.4 | 데이터 검증 |

### 1.3 데이터 흐름도

```
[사용자 요청] → [FastAPI Server] → [ForgeAgent]
                                       ↓
                            [Browser Factory] ← [Playwright]
                                       ↓
                            [DOM Scraping + Screenshot]
                                       ↓
                            [Bounding Box 그리기]
                                       ↓
                            [LLM API Handler] → [LLM Provider]
                                       ↓
                            [Action 파싱 및 실행]
                                       ↓
                            [결과 반환]
```

---

## 2. 핵심 모듈 상세 분석

### 2.1 [MANUS_FEATURE] 시각적 인식 엔진

> **Manus AI처럼 화면에 Bounding Box + 번호 라벨을 그리는 기능**

#### 파일 위치

| 파일 | 역할 |
|------|------|
| `skyvern/webeye/scraper/domUtils.js` | Bounding Box 그리기 (JavaScript) |
| `skyvern/webeye/utils/page.py` | Python에서 JS 함수 호출 |

#### 핵심 함수

**`domUtils.js` (라인 1907-1918)**
```javascript
function drawBoundingBoxes(elements) {
  // draw a red border around the elements
  DomUtils.clearVisibleClientRectCache();
  elements.forEach((element) => {
    const ele = getDOMElementBySkyvenElement(element);
    element.rect = ele ? DomUtils.getVisibleClientRect(ele, true) : null;
  });
  var groups = groupElementsVisually(elements);
  var hintMarkers = createHintMarkersForGroups(groups);
  addHintMarkersToPage(hintMarkers);
  DomUtils.clearVisibleClientRectCache();
}
```

**`createHintMarkerForGroup()` (라인 2112-2148) - Bounding Box 스타일 정의**
```javascript
function createHintMarkerForGroup(group) {
  // ... 스크롤 위치 계산 ...

  // Bounding Box 스타일 설정
  boundingBox.style.border = "2px solid blue";  // ★ 테두리 색상 변경 지점
  boundingBox.style.pointerEvents = "none";
  boundingBox.style.zIndex = this.currentZIndex++;

  return Object.assign(marker, {
    element: el,        // 라벨 요소
    boundingBox: boundingBox,  // 박스 요소
    group: group,
  });
}
```

#### 호출 체인

```
Python: SkyvernFrame.build_elements_and_draw_bounding_boxes()
    ↓
JavaScript: buildElementsAndDrawBoundingBoxes()
    ↓
JavaScript: drawBoundingBoxes(elements)
    ↓
JavaScript: createHintMarkersForGroups(groups)
    ↓
JavaScript: addHintMarkersToPage(hintMarkers)
```

#### 수정 가이드: Bounding Box 스타일 변경

```javascript
// 파일: skyvern/webeye/scraper/domUtils.js (라인 2139)

// 변경 전
boundingBox.style.border = "2px solid blue";

// 변경 후 (Manus AI 스타일로)
boundingBox.style.border = "3px solid #FF6B35";  // 주황색 테두리
boundingBox.style.borderRadius = "4px";
boundingBox.style.boxShadow = "0 0 10px rgba(255, 107, 53, 0.5)";
```

#### 라벨 스타일 변경 (라인 2119-2126)

```javascript
// 라벨 요소 스타일 추가
el.style.backgroundColor = "#FF6B35";
el.style.color = "white";
el.style.padding = "2px 6px";
el.style.borderRadius = "4px";
el.style.fontSize = "12px";
el.style.fontWeight = "bold";
```

---

### 2.2 [BRAIN_CONFIG] LLM Provider 설정

#### 파일 위치

| 파일 | 역할 |
|------|------|
| `skyvern/config.py` (라인 146-320) | 환경 변수 및 LLM 설정 |
| `skyvern/forge/sdk/api/llm/config_registry.py` | LLM 설정 레지스트리 |
| `skyvern/forge/sdk/api/llm/api_handler_factory.py` | LLM 핸들러 생성 |

#### Ollama 연동 (이미 구현됨!)

**`config.py` (라인 300-302)**
```python
ENABLE_OLLAMA: bool = False
OLLAMA_SERVER_URL: str | None = None
OLLAMA_MODEL: str | None = None
```

**`config_registry.py` (라인 1331-1349)**
```python
if settings.ENABLE_OLLAMA:
    if settings.OLLAMA_MODEL:
        ollama_model_name = settings.OLLAMA_MODEL
        LLMConfigRegistry.register_config(
            "OLLAMA",
            LLMConfig(
                f"ollama/{ollama_model_name}",
                ["OLLAMA_SERVER_URL", "OLLAMA_MODEL"],
                supports_vision=False,  # ★ Ollama는 현재 Vision 미지원
                add_assistant_prefix=False,
                litellm_params=LiteLLMParams(
                    api_base=settings.OLLAMA_SERVER_URL,
                    ...
                ),
            ),
        )
```

#### Ollama 연동 방법 (.env 설정)

```bash
# .env 파일에 추가
ENABLE_OLLAMA=true
OLLAMA_SERVER_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2-vision  # 또는 qwen2-vl
LLM_KEY=OLLAMA
```

#### GPT-4o-mini 연동 (이미 구현됨!)

**`config_registry.py` (라인 187-195)**
```python
LLMConfigRegistry.register_config(
    "OPENAI_GPT4O_MINI",
    LLMConfig(
        "gpt-4o-mini",
        ["OPENAI_API_KEY"],
        supports_vision=True,
        add_assistant_prefix=False,
        max_completion_tokens=16384,
    ),
)
```

#### GPT-4o-mini 연동 방법 (.env 설정)

```bash
# .env 파일에 추가
ENABLE_OPENAI=true
OPENAI_API_KEY=sk-xxx
LLM_KEY=OPENAI_GPT4O_MINI
```

---

### 2.3 [HANDS_CONTROL] 브라우저 제어

#### 파일 위치

| 파일 | 역할 |
|------|------|
| `skyvern/webeye/browser_factory.py` | 브라우저 생성/관리 |
| `skyvern/webeye/actions/handler.py` | 액션 실행 (클릭, 입력 등) |
| `skyvern/config.py` (라인 34-45) | 브라우저 설정 |

#### Chrome 프로필 로드 (핵심!)

**`browser_factory.py` (라인 510-518) - 기본 Chrome 프로필 경로**
```python
def default_user_data_dir() -> pathlib.Path:
    p = platform.system()
    if p == "Darwin":
        return pathlib.Path("~/Library/Application Support/Google/Chrome").expanduser()
    if p == "Windows":
        return pathlib.Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"
    # Assume Linux/Unix
    return pathlib.Path("~/.config/google-chrome").expanduser()
```

**`browser_factory.py` (라인 559-590) - CDP 연결 시 프로필 로드**
```python
# Chrome 실행 시 user_data_dir 지정
browser_process = subprocess.Popen(
    [
        browser_path,
        "--remote-debugging-port=9222",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-debugging-address=0.0.0.0",
        "--user-data-dir=./tmp/user_data_dir",  # ★ 프로필 경로
    ],
    ...
)
```

**`browser_factory.py` (라인 453-472) - Headless 브라우저 생성**
```python
user_data_dir = make_temp_directory(prefix="skyvern_browser_")
# ...
browser_args.update({
    "user_data_dir": user_data_dir,  # ★ 임시 프로필 사용
    "downloads_path": download_dir,
})
browser_context = await playwright.chromium.launch_persistent_context(**browser_args)
```

#### 기존 Chrome 프로필 연동 방법

**방법 1: CDP 연결 (추천)**

```bash
# 1. Chrome을 직접 실행 (기존 프로필 사용)
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.config/google-chrome"

# 2. .env 설정
BROWSER_TYPE=chromium-cdp
BROWSER_REMOTE_DEBUGGING_URL=http://127.0.0.1:9222
```

**방법 2: 코드 수정 (browser_factory.py 라인 453-472)**

```python
# 변경 전
user_data_dir = make_temp_directory(prefix="skyvern_browser_")

# 변경 후
user_data_dir = "/path/to/your/chrome/profile"  # 기존 프로필 경로
```

#### 브라우저 설정 (config.py)

```python
BROWSER_TYPE: str = "chromium-headful"  # 또는 "chromium-cdp"
BROWSER_REMOTE_DEBUGGING_URL: str = "http://127.0.0.1:9222"
BROWSER_WIDTH: int = 1920
BROWSER_HEIGHT: int = 1080
BROWSER_LOCALE: str = "ko-KR"  # 한국어 로케일
```

---

### 2.4 [TASK_ENGINE] 워크플로우 엔진

#### 파일 위치

| 파일 | 역할 |
|------|------|
| `skyvern/forge/agent.py` | ForgeAgent - 메인 에이전트 |
| `skyvern/forge/sdk/workflow/models/block.py` | Block 시스템 |
| `skyvern/forge/sdk/workflow/service.py` | 워크플로우 서비스 |

#### Block 타입 (block.py)

| Block 타입 | 역할 |
|------------|------|
| `NavigationBlock` | URL 네비게이션 |
| `ActionBlock` | 웹 액션 실행 (클릭, 입력 등) |
| `ExtractionBlock` | 데이터 추출 |
| `ValidationBlock` | 결과 검증 |
| `ForLoopBlock` | 반복 실행 |
| `CodeBlock` | Python 코드 실행 |
| `WaitBlock` | 대기 |
| `UploadFileBlock` | 파일 업로드 |

#### 네이버 블로그 자동화 워크플로우 예시

```python
# 워크플로우 정의 (YAML/JSON)
workflow:
  blocks:
    - type: NavigationBlock
      url: "https://blog.naver.com"

    - type: ActionBlock
      label: "로그인 확인"
      complete_criterion: "로그인 상태 확인"

    - type: ActionBlock
      label: "글쓰기 버튼 클릭"
      goal: "글쓰기 버튼을 찾아 클릭"

    - type: UploadFileBlock
      label: "이미지 업로드"
      file_path: "/path/to/image.jpg"

    - type: ActionBlock
      label: "본문 입력"
      goal: "에디터에 본문 텍스트 입력"
      data_extraction_goal: "포스팅 완료 확인"
```

---

### 2.5 [CONFIG_MAP] 설정 파일 구조

#### 주요 설정 파일

| 파일 | 역할 |
|------|------|
| `.env` | 환경 변수 (API 키, 설정값) |
| `skyvern/config.py` | Pydantic Settings 기반 설정 클래스 |
| `skyvern/webeye/chromium_preferences.json` | 브라우저 기본 설정 |

#### 필수 환경 변수 (.env)

```bash
# 필수
DATABASE_STRING=postgresql+psycopg://skyvern@localhost/skyvern

# LLM 설정 (택1)
ENABLE_OPENAI=true
OPENAI_API_KEY=sk-xxx
LLM_KEY=OPENAI_GPT4O_MINI

# 또는 Ollama
ENABLE_OLLAMA=true
OLLAMA_SERVER_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2-vision
LLM_KEY=OLLAMA

# 브라우저 설정
BROWSER_TYPE=chromium-headful
BROWSER_WIDTH=1920
BROWSER_HEIGHT=1080
BROWSER_LOCALE=ko-KR
```

---

## 3. 커스터마이징 가이드

### 3.1 네이버 블로그 자동화를 위한 수정 사항

#### 1) Chrome 프로필 연동 (캡차 우회)

```bash
# .env
BROWSER_TYPE=chromium-cdp
BROWSER_REMOTE_DEBUGGING_URL=http://127.0.0.1:9222
```

```bash
# Chrome 실행 스크립트
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.config/google-chrome" \
  --profile-directory="Default"
```

#### 2) Ollama Vision 모델 연동

```bash
# .env
ENABLE_OLLAMA=true
OLLAMA_SERVER_URL=http://localhost:11434
OLLAMA_MODEL=llava:13b
LLM_KEY=OLLAMA
```

**config_registry.py 수정 필요** (Vision 지원 활성화):
```python
# 라인 1337
supports_vision=True,  # False에서 True로 변경
```

#### 3) Bounding Box 시각적 스타일 개선

```javascript
// skyvern/webeye/scraper/domUtils.js (라인 2128-2148)

// boundingBox 스타일 수정
boundingBox.style.border = "3px solid #FF6B35";
boundingBox.style.borderRadius = "4px";
boundingBox.style.boxShadow = "0 0 10px rgba(255, 107, 53, 0.5)";
boundingBox.style.backgroundColor = "rgba(255, 107, 53, 0.1)";

// 라벨 스타일 수정 (el 요소)
el.style.backgroundColor = "#FF6B35";
el.style.color = "white";
el.style.padding = "4px 8px";
el.style.borderRadius = "4px";
el.style.fontSize = "14px";
el.style.fontWeight = "bold";
el.style.fontFamily = "Arial, sans-serif";
```

---

## 4. 주의사항 및 검증 포인트

### 4.1 확인된 사항

- [x] Bounding Box 그리기 코드 위치 확인
- [x] Ollama 연동 코드 이미 구현됨 (config_registry.py)
- [x] GPT-4o-mini 연동 코드 이미 구현됨
- [x] Chrome 프로필 로드 지원 (CDP 모드)
- [x] 워크플로우 Block 시스템 확인

### 4.2 주의 사항

1. **Ollama Vision 미지원**: 현재 `supports_vision=False`로 설정됨
   - 수정 필요: `config_registry.py` 라인 1337

2. **Chrome 프로필 동시 접근 불가**: 기존 Chrome이 실행 중이면 같은 프로필 사용 불가
   - 해결책: 프로필 복사 후 사용 또는 Chrome 종료 후 실행

3. **네이버 스마트에디터**: iframe 내부 작업 필요
   - Skyvern이 iframe 진입 지원함 (`SkyvernFrame` 클래스)

### 4.3 버전 호환성

- Python: 3.11+ 필수
- Playwright: 최신 버전 권장
- Node.js: 18+ (프론트엔드)

---

## 부록: 빠른 참조 표

| 기능 | 파일 경로 | 핵심 함수/설정 | 설정 위치 |
|------|-----------|----------------|-----------|
| Bounding Box 그리기 | `skyvern/webeye/scraper/domUtils.js` | `drawBoundingBoxes()` | 라인 1907-1918 |
| Box 스타일 | `skyvern/webeye/scraper/domUtils.js` | `createHintMarkerForGroup()` | 라인 2128-2148 |
| LLM 설정 | `skyvern/config.py` | `LLM_KEY`, `ENABLE_*` | 라인 146-320 |
| Ollama 연동 | `skyvern/forge/sdk/api/llm/config_registry.py` | `ENABLE_OLLAMA` | 라인 1331-1349 |
| GPT-4o-mini | `skyvern/forge/sdk/api/llm/config_registry.py` | `OPENAI_GPT4O_MINI` | 라인 187-195 |
| 브라우저 생성 | `skyvern/webeye/browser_factory.py` | `launch_persistent_context()` | 라인 471 |
| Chrome 프로필 | `skyvern/webeye/browser_factory.py` | `default_user_data_dir()` | 라인 510-517 |
| 액션 실행 | `skyvern/webeye/actions/handler.py` | `ActionHandler` | 전체 |
| 워크플로우 | `skyvern/forge/sdk/workflow/models/block.py` | `Block` 클래스들 | 전체 |

---

**작성일**: 2025-11-28
**분석 도구**: Claude Code (Opus 4)
