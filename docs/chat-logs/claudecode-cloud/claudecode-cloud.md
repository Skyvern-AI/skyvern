**shinjadong/skyvern**

최적화된 프롬프트 <system> 당신은 **오픈소스 코드베이스 분석 전문가**입니다.

* 대규모 Python 프로젝트의 아키텍처를 역공학(Reverse Engineering)하는 능력
* 코드 흐름을 추적하여 "이 기능은 어디서 시작해서 어디서 끝나는지" 매핑
* 개발자가 즉시 수정 가능한 수준의 구체적 파일/함수/라인 지목 </system>

<mission> \*\*Skyvern\*\* (https://github.com/Skyvern-AI/skyvern) 리포지토리를 분석하여, "네이버 블로그 자동화" 프로젝트에 필요한 \*\*핵심 코드 위치\*\*와 \*\*수정 포인트\*\*를 개발자가 즉시 활용할 수 있는 \*\*코드 지도(Code Map)\*\*로 작성하십시오. </mission> <context> <project\_goal> - 최종 목표: 네이버 블로그 자동 포스팅 (로그인 유지, 스마트에디터 제어, 이미지 업로드) - 핵심 요구사항: 1. 기존 Chrome 프로필 로드 (캡차 우회) 2. 시각적 디버깅 (Manus AI처럼 박스+번호 오버레이) 3. Local LLM(Ollama) 또는 저비용 모델(GPT-4o-mini) 연동 </project\_goal><user\_interest> 사용자는 특히 \*\*"Manus AI"\*\*처럼 화면에 **Bounding Box + Label**을 그려서 "AI가 뭘 보고 있는지" 시각화하는 기능에 큰 관심이 있습니다. 이 기능의 **정확한 구현 위치**를 반드시 찾아주십시오. </user\_interest>

<analysis\_constraints>

* 분석 기준: **최신 main 브랜치** (커밋 해시 명시 권장)
* 언어: Python 3.10+ 기준
* 추정이 아닌 **실제 파일 경로**만 제시 (존재 여부 검증 필수)
* 파일이 없거나 구조가 다르면 "확인 필요" 표기 </analysis\_constraints> </context>

<analysis\_methodology> 다음 순서로 체계적으로 분석하십시오:

<step\_1 name="전체 구조 파악">

1. 리포지토리 클론 또는 GitHub 탐색
2. 최상위 디렉토리 구조 매핑
3. `<span><span>pyproject.toml</span></span>`, `<span><span>setup.py</span></span>`, `<span><span>requirements.txt</span></span>`로 핵심 의존성 파악
4. `<span><span>README.md</span></span>`, `<span><span>docs/</span></span>` 폴더로 공식 아키텍처 문서 확인 </step\_1>

<step\_2 name="엔트리포인트 추적">

1. `<span><span>main.py</span></span>` 또는 `<span><span>__main__.py</span></span>` 찾기
2. CLI 명령어 정의 위치 (`<span><span>click</span></span>`, `<span><span>typer</span></span>`, `<span><span>argparse</span></span>` 사용 여부)
3. 서버 실행 시 호출되는 첫 함수 추적 </step\_2>

<step\_3 name="핵심 모듈 분석"> 각 타겟 기능에 대해:

1. 키워드 검색 (예: "bounding\_box", "overlay", "screenshot")
2. 클래스/함수 정의 위치 확인
3. 호출 체인(Call Chain) 역추적
4. 설정 파일과의 연결점 파악 </step\_3>

<step\_4 name="수정 포인트 도출">

1. 설정만으로 변경 가능한 것 vs 코드 수정 필요한 것 분류
2. 수정 시 영향 범위(Side Effect) 분석
3. 테스트 코드 존재 여부 확인 </step\_4> </analysis\_methodology>

<analysis\_targets> 우선순위 순으로 분석하십시오:

<target\_1 name="시각적 인식 엔진" priority="최우선" tag="MANUS\_FEATURE"> **질문**: 웹페이지에서 상호작용 가능한 요소를 탐지하고, 스크린샷 위에 **Bounding Box + 번호 라벨**을 그리는 코드는 어디에 있습니까?

**찾아야 할 것**:

* 스크린샷 캡처 함수
* 요소 탐지 로직 (DOM 기반 vs Vision 기반)
* 오버레이 렌더링 함수 (PIL, OpenCV, 또는 브라우저 inject)
* 디버깅 모드 ON/OFF 설정 위치

**예상 키워드**: `<span><span>bounding_box</span></span>`, `<span><span>overlay</span></span>`, `<span><span>annotate</span></span>`, `<span><span>highlight</span></span>`, `<span><span>draw_rect</span></span>`, `<span><span>label</span></span>` </target\_1>

<target\_2 name="LLM Provider 설정" priority="높음" tag="BRAIN\_CONFIG"> **질문**: OpenAI 대신 **Ollama** 또는 **GPT-4o-mini**로 변경하려면 어떤 파일의 어떤 값을 수정해야 합니까?

**찾아야 할 것**:

* LLM 클라이언트 추상화 인터페이스 (Provider Pattern)
* 모델명/엔드포인트 설정 위치
* API 키 환경변수 매핑
* Ollama 지원 여부 (이미 구현? 추가 필요?)

**예상 키워드**: `<span><span>llm_provider</span></span>`, `<span><span>openai</span></span>`, `<span><span>model</span></span>`, `<span><span>client</span></span>`, `<span><span>completion</span></span>` </target\_2>

<target\_3 name="브라우저 제어" priority="높음" tag="HANDS\_CONTROL"> **질문**: 클릭, 타이핑, 스크롤, 파일 업로드를 수행하는 **Playwright 래퍼 코드**는 어디에 있습니까?

**찾아야 할 것**:

* Playwright 초기화 및 브라우저 실행 코드
* **Chrome 프로필(User Data Dir) 로드 설정 위치** ★
* 각 액션(click, type, scroll, upload) 함수 정의
* iFrame 진입/탈출 처리 로직

**예상 키워드**: `<span><span>playwright</span></span>`, `<span><span>browser</span></span>`, `<span><span>page</span></span>`, `<span><span>click</span></span>`, `<span><span>fill</span></span>`, `<span><span>user_data_dir</span></span>`, `<span><span>launch</span></span>` </target\_3>

<target\_4 name="워크플로우 엔진" priority="중간" tag="TASK\_ENGINE"> **질문**: YAML/JSON 태스크 파일을 파싱하고 순차 실행하는 \*\*메인 루프(Orchestrator)\*\*는 어디에 있습니까?

**찾아야 할 것**:

* 태스크 파일 스키마 정의
* 파싱 로직
* 실행 루프 (while/for)
* 에러 핸들링 및 재시도 로직

**예상 키워드**: `<span><span>task</span></span>`, `<span><span>workflow</span></span>`, `<span><span>execute</span></span>`, `<span><span>step</span></span>`, `<span><span>yaml</span></span>`, `<span><span>schema</span></span>` </target\_4>

<target\_5 name="설정 파일 구조" priority="중간" tag="CONFIG\_MAP"> **질문**: 전체 시스템 설정을 제어하는 **Config 파일들**의 위치와 각 설정 항목의 역할은 무엇입니까?

**찾아야 할 것**:

* `<span><span>.env</span></span>`, `<span><span>config.yaml</span></span>`, `<span><span>settings.py</span></span>` 등
* 환경별(dev/prod) 설정 분리 여부
* 필수 vs 선택 설정 항목 구분 </target\_5> </analysis\_targets>

<output\_format> 다음 구조로 결과를 작성하십시오:

<structure> # Skyvern 코드베이스 분석 보고서 \*\*분석 기준\*\*: [커밋 해시 또는 날짜] \*\*리포지토리\*\*: https://github.com/Skyvern-AI/skyvern---

## 1. 전체 아키텍처 개요

### 1.1 디렉토리 구조

skyvern/ ├── [폴더명]/ # [한 줄 설명] │ ├── [파일명].py # [역할] ...

### 1.2 핵심 의존성


| 패키지     | 버전 | 역할            |
| ---------- | ---- | --------------- |
| playwright | x.x  | 브라우저 자동화 |
| ...        | ...  | ...             |

### 1.3 데이터 흐름도

## [입력] → [처리1] → [처리2] → [출력] ↓ [LLM 호출]

## 2. 핵심 모듈 상세 분석

### 2.1 [MANUS\_FEATURE] 시각적 인식 엔진

**파일 위치**: `<span><span>skyvern/path/to/file.py</span></span>` **핵심 함수**:

* `<span><span class="token">function_name</span><span class="token">(</span><span class="token">)</span></span>` (라인 XX-XX): [역할 설명]

**코드 스니펫**:

```python
# 파일: skyvern/path/to/file.py (라인 123-145)
def draw_bounding_boxes(...):
    """
    실제 코드 또는 핵심 로직 요약
    """
호출 체인:
main() → agent.run() → vision.analyze() → draw_overlay()
수정 가이드:
오버레이 색상 변경: 파일명.py 라인 XX의 COLOR 상수
라벨 폰트 변경: ...
2.2 [BRAIN_CONFIG] LLM Provider
[동일 형식으로 반복]
3. 커스터마이징 가이드
3.1 Chrome 프로필 연동 방법
수정 파일: skyvern/path/to/browser.py
수정 내용:
# 변경 전
browser = await playwright.chromium.launch()

# 변경 후
browser = await playwright.chromium.launch_persistent_context(
    user_data_dir="/path/to/chrome/profile",
    # ... 추가 옵션
)
3.2 Ollama 연동 방법
[구체적 수정 가이드]
4. 주의사항 및 검증 포인트
4.1 확인되지 않은 사항
[추정했지만 검증 필요한 내용]
4.2 버전 호환성
Python: X.X 이상 필요
Playwright: X.X 버전에서 테스트됨
부록: 빠른 참조 표
기능
파일 경로
핵심 함수
설정 위치
시각적 인식
skyvern/...
draw_boxes()
config.yaml
LLM 호출
skyvern/...
call_llm()
.env
브라우저 제어
skyvern/...
execute_action()
browser.py




<code_snippet_rules>
코드 스니펫 포함 기준:
포함: 수정이 필요한 핵심 로직, 설정 변경점
제외: 단순 import문, 보일러플레이트, 100줄 이상의 코드
형식: 파일 경로 + 라인 번호 + 핵심 부분만 발췌
주석: 코드 내 한글 주석으로 역할 설명
</code_snippet_rules>
</output_format>
<quality_checklist>
작성 완료 후 자체 검증:
□ 모든 파일 경로가 실제 존재하는지 확인했는가?
□ "추정" vs "확인"이 명확히 구분되어 있는가?
□ Chrome 프로필 연동 방법이 복사-붙여넣기 가능한 수준으로 구체적인가?
□ 시각적 디버깅(Bounding Box) 기능의 정확한 위치를 찾았는가?
□ LLM Provider 변경 방법이 단계별로 설명되어 있는가?
□ 호출 체인이 시작점부터 끝점까지 추적되어 있는가?
□ 수정 시 사이드 이펙트 경고가 포함되어 있는가?
</quality_checklist>
```

# Skyvern 코드베이스 분석 시작

네이버 블로그 자동화 프로젝트에 필요한 핵심 코드 위치를 찾기 위해 체계적으로 분석하겠습니다.

**Update Todos**

전체 디렉토리 구조 분석 중

[MANUS\_FEATURE] 시각적 인식 엔진 - Bounding Box/오버레이 코드 위치 찾기

[BRAIN\_CONFIG] LLM Provider 설정 - Ollama/GPT-4o-mini 연동 포인트 찾기

[HANDS\_CONTROL] 브라우저 제어 - Chrome 프로필 로드 설정 찾기

[TASK\_ENGINE] 워크플로우 엔진 분석

[CONFIG\_MAP] 설정 파일 구조 매핑

최종 분석 보고서 작성 및 커밋

**Bash**find . -maxdepth 2 -type d | grep -v \_\_pycache\_\_ | grep -v '.git' | grep -v node\_modules | grep -v '.next' | sort

<details class="font-mono whitespace-pre-wrap [&[open]>summary]:hidden text-text-100"><summary class="cursor-pointer list-none"><span>./skyvern/webeye
./tests
./tests/unit_tests</span><span class="block text-text-500">... +47 라인</span></summary>

</details>

**Read**/home/user/skyvern/pyproject.toml

207줄 읽음

**Bash**git log -1 --format="%H %s" 2>/dev/null || echo "Git info unavailable"

5b530cab52a4ae070b2e9c844dc4c91808a0ca1b Initial scenario tests infra (#4118)

**Glob**skyvern/\*\*/\*.py

**Grep**bounding\_box|draw\_rect|overlay|annotate|highlight

**Grep**user\_data\_dir|launch\_persistent|chrome.\*profile

**Read**/home/user/skyvern/skyvern/webeye/utils/dom.py

1,053줄 읽음

**Read**/home/user/skyvern/skyvern/webeye/browser\_factory.py

1,054줄 읽음

**Grep**litellm|llm\_provider|LLM\_KEY|model.\*config

**Update Todos**

전체 디렉토리 구조 및 핵심 의존성 파악

시각적 인식 엔진 분석 중

[BRAIN\_CONFIG] LLM Provider 설정 - Ollama/GPT-4o-mini 연동 포인트 찾기

[HANDS\_CONTROL] 브라우저 제어 - Chrome 프로필 로드 설정 찾기

[TASK\_ENGINE] 워크플로우 엔진 분석

[CONFIG\_MAP] 설정 파일 구조 매핑

최종 분석 보고서 작성 및 커밋

**Read**/home/user/skyvern/skyvern/webeye/utils/page.py

547줄 읽음

**Read**/home/user/skyvern/skyvern/config.py

522줄 읽음

**Read**/home/user/skyvern/skyvern/forge/sdk/api/llm/api\_handler\_factory.py

1,495줄 읽음

핵심 파일들을 발견했습니다. 이제 Bounding Box를 그리는 JavaScript 코드와 Scraper 코드를 더 분석하겠습니다.

**Read**/home/user/skyvern/skyvern/webeye/scraper/domUtils.js

File content (25148 tokens) exceeds maximum allowed tokens (25000). Please use offset and limit parameters to read specific portions of the file, or use the GrepTool to search for specific content.

**Read**/home/user/skyvern/skyvern/forge/sdk/api/llm/config\_registry.py

1,457줄 읽음

**Grep**drawBoundingBox|buildElementsAndDrawBoundingBoxes|removeBoundingBoxes|createLabel

**Read**/home/user/skyvern/skyvern/webeye/scraper/domUtils.js

200줄 읽음

**Grep**createHintMarkersForGroups|createStackedMarkers|hintMarker|boundingBox

**Update Todos**

전체 디렉토리 구조 및 핵심 의존성 파악

[MANUS\_FEATURE] 시각적 인식 엔진 - Bounding Box/오버레이 코드 위치 찾기

[BRAIN\_CONFIG] LLM Provider 설정 - Ollama/GPT-4o-mini 연동 포인트 찾기

브라우저 제어 코드 분석 중

[TASK\_ENGINE] 워크플로우 엔진 분석

[CONFIG\_MAP] 설정 파일 구조 매핑

최종 분석 보고서 작성 및 커밋

**Read**/home/user/skyvern/skyvern/webeye/browser\_factory.py

300줄 읽음

**Read**/home/user/skyvern/skyvern/webeye/actions/handler.py

200줄 읽음

**Grep**user\_data\_dir|launch\_persistent|cdp\_url|existing\_browser

이제 Agent와 워크플로우 엔진을 분석하겠습니다.

**Glob**skyvern/agent/\*\*/\*.py

**Read**/home/user/skyvern/skyvern/agent/agent.py

<tool\_use\_error>File does not exist.</tool\_use\_error>

**Glob**skyvern/forge/\*\*/\*.py

**Grep**class.\*Agent|AgentStep|execute\_step

**Read**/home/user/skyvern/skyvern/forge/agent.py

200줄 읽음

**Glob**skyvern/forge/sdk/workflow/\*\*/\*.py

**Update Todos**

전체 디렉토리 구조 및 핵심 의존성 파악

[MANUS\_FEATURE] 시각적 인식 엔진 - Bounding Box/오버레이 코드 위치 찾기

[BRAIN\_CONFIG] LLM Provider 설정 - Ollama/GPT-4o-mini 연동 포인트 찾기

[HANDS\_CONTROL] 브라우저 제어 - Chrome 프로필 로드 설정 찾기

워크플로우 엔진 분석 중

[CONFIG\_MAP] 설정 파일 구조 매핑

최종 분석 보고서 작성 및 커밋

**Read**/home/user/skyvern/skyvern/forge/sdk/workflow/models/block.py

200줄 읽음

**Read**/home/user/skyvern/skyvern/config.py

300줄 읽음

**Read**/home/user/skyvern/skyvern/config.py

150줄 읽음

이제 충분한 정보를 수집했습니다. 최종 분석 보고서를 작성하겠습니다.

**Update Todos**

전체 디렉토리 구조 및 핵심 의존성 파악

[MANUS\_FEATURE] 시각적 인식 엔진 - Bounding Box/오버레이 코드 위치 찾기

[BRAIN\_CONFIG] LLM Provider 설정 - Ollama/GPT-4o-mini 연동 포인트 찾기

[HANDS\_CONTROL] 브라우저 제어 - Chrome 프로필 로드 설정 찾기

[TASK\_ENGINE] 워크플로우 엔진 분석

[CONFIG\_MAP] 설정 파일 구조 매핑

분석 보고서 작성 중

**Write**/home/user/skyvern/SKYVERN\_ARCHITECTURE\_ANALYSIS.md

`# Skyvern 코드베이스 분석 보고서`

` `

`**분석 기준**: 커밋 `5b530ca` (2025-11-28)`

`**리포지토리**: https://github.com/Skyvern-AI/skyvern`

`**분석 목적**: 네이버 블로그 자동화 프로젝트를 위한 핵심 코드 위치 파악`

` `

`---`

` `

`## 1. 전체 아키텍처 개요`

` `

`### 1.1 디렉토리 구조`

` `

`````



`skyvern/`



`├── cli/                    # CLI 명령어 (skyvern run, skyvern init 등)`



`├── config.py               # 전체 설정 관리 (LLM, 브라우저, 프록시 등)`



`├── forge/                  # 핵심 비즈니스 로직`



`│   ├── agent.py            # ForgeAgent - 메인 에이전트 클래스`



`│   ├── agent_functions.py  # 에이전트 함수들`



`│   ├── api_app.py          # FastAPI 서버`






Show full diff (506 more lines)









**Bash**git status

<details class="font-mono whitespace-pre-wrap [&[open]>summary]:hidden text-text-100"><summary class="cursor-pointer list-none"><span>	SKYVERN_ARCHITECTURE_ANALYSIS.md

nothing added to commit but untracked files present (use "git add" to track)</span><span class="block text-text-500">... +3 라인</span></summary>


</details>







**Bash**git add SKYVERN\_ARCHITECTURE\_ANALYSIS.md && git commit -m "\$(cat <<'EOF' Add comprehensive Skyvern codebase analysis report Analysis covers key code locations for: - Visual recognition engine (Bounding Box rendering) - LLM Provider configuration (Ollama, GPT-4o-mini) - Browser control (Chrome profile loading) - Workflow engine (Block system) - Configuration structure Purpose: Support Naver blog automation project EOF )"

[claude/analyze-skyvern-architecture-01NX7mN8NWfWdASX7CUbsFun 14dcc6e] Add comprehensive Skyvern codebase analysis report  1 file changed, 525 insertions(+)  create mode 100644 SKYVERN\_ARCHITECTURE\_ANALYSIS.md







**Bash**git push -u origin claude/analyze-skyvern-architecture-01NX7mN8NWfWdASX7CUbsFun

<details class="font-mono whitespace-pre-wrap [&[open]>summary]:hidden text-text-100"><summary class="cursor-pointer list-none"><span>remote: 
To http://127.0.0.1:46619/git/shinjadong/skyvern
 * [new branch]      claude/analyze-skyvern-architecture-01NX7mN8NWfWdASX7CUbsFun -> claude/analyze-skyvern-architecture-01NX7mN8NWfWdASX7CUbsFun</span><span class="block text-text-500">... +4 라인</span></summary>


</details>







**Update Todos**

 전체 디렉토리 구조 및 핵심 의존성 파악

 [MANUS\_FEATURE] 시각적 인식 엔진 - Bounding Box/오버레이 코드 위치 찾기

 [BRAIN\_CONFIG] LLM Provider 설정 - Ollama/GPT-4o-mini 연동 포인트 찾기

 [HANDS\_CONTROL] 브라우저 제어 - Chrome 프로필 로드 설정 찾기

 [TASK\_ENGINE] 워크플로우 엔진 분석

 [CONFIG\_MAP] 설정 파일 구조 매핑

 최종 분석 보고서 작성 및 커밋








# Skyvern 코드베이스 분석 완료

분석 보고서를 `<span><span>SKYVERN_ARCHITECTURE_ANALYSIS.md</span></span>`에 작성하고 커밋했습니다.

## 핵심 발견 사항 요약

### 1. Manus AI 스타일 Bounding Box (최우선)

| 항목                  | 위치                                                                        |
| ----------------------- | ----------------------------------------------------------------------------- |
| **핵심 파일**   | `<span><span>skyvern/webeye/scraper/domUtils.js</span></span>`          |
| **그리기 함수** | `<span><span>drawBoundingBoxes()</span></span>` (라인 1907)             |
| **스타일 정의** | `<span><span>createHintMarkerForGroup()</span></span>` (라인 2128-2148) |
| **박스 색상**   | `<span><span>boundingBox.style.border = "2px solid blue"</span></span>` |


### 2. LLM Provider 연동

**Ollama (이미 구현됨!)**

```bash
# .env
ENABLE_OLLAMA=true
OLLAMA_SERVER_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2-vision
LLM_KEY=OLLAMA
```




**GPT-4o-mini (이미 구현됨!)**

```bash
# .env
ENABLE_OPENAI=true
OPENAI_API_KEY=sk-xxx
LLM_KEY=OPENAI_GPT4O_MINI
```




### 3. Chrome 프로필 로드 (캡차 우회)

**CDP 모드로 기존 프로필 사용:**

```bash
# Chrome 실행
google-chrome --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.config/google-chrome"

# .env
BROWSER_TYPE=chromium-cdp
BROWSER_REMOTE_DEBUGGING_URL=http://127.0.0.1:9222
```




### 4. 주요 수정 포인트

| 기능                 | 파일                                               | 라인                                                        |
| ---------------------- | ---------------------------------------------------- | ------------------------------------------------------------- |
| Box 색상 변경        | `<span><span>domUtils.js</span></span>`        | 2139                                                        |
| Ollama Vision 활성화 | `<span><span>config_registry.py</span></span>` | 1337 (`<span><span>supports_vision=True</span></span>`) |
| 프로필 경로 변경     | `<span><span>browser_factory.py</span></span>` | 453-472                                                     |


상세 내용은 `<span><span>SKYVERN_ARCHITECTURE_ANALYSIS.md</span></span>` 파일을 참조하세요.






네이버 블로그포스팅자동화가 현재 목적이지만 중단기적으로 바로 프로덕션서비스로 진행하고싶어졌어 . 한국 최적화된 자동화 플랫폼SaaS로 쓰기에 독보적이야.어쨋든, 여기 다 오픈소스니까, 프론트는 내 버셀로 띄우고, 백엔드는 우리집 WSL에서 작동하게 하고싶어. 어떻게하면될까? Ultrathink





이건 정말 좋은 아이디어입니다. Skyvern을 한국 시장에 특화된 SaaS로 전환하는 것은 매우 유망합니다.

현재 구조와 배포 방안을 깊이 분석해보겠습니다.





**Update Todos**

 프론트엔드/백엔드 아키텍처 분석 중

 Vercel 프론트엔드 배포 전략 수립

 WSL 백엔드 설정 및 외부 노출 방안

 프로덕션 아키텍처 설계 문서 작성









This session is being continued from a previous conversation that ran out of context. The conversation is summarized below: Analysis: Let me analyze the conversation chronologically:

1. **Initial Request**: The user provided an extensive Korean prompt asking for a comprehensive code analysis of the Skyvern repository. The goal was to create a "Code Map" for a Naver blog automation project with specific requirements:
   * Chrome profile loading (captcha bypass)
   * Visual debugging (Manus AI-style bounding box + number overlay)
   * Local LLM (Ollama) or low-cost model (GPT-4o-mini) integration
1. **Analysis Process**: I systematically analyzed:
   * Directory structure and dependencies (pyproject.toml)
   * Bounding Box rendering code in domUtils.js
   * LLM configuration in config.py and config\_registry.py
   * Browser factory for Chrome profile loading
   * Workflow engine and block system
1. **Key Files Examined**:
   * `<span><span>skyvern/webeye/scraper/domUtils.js</span></span>` - Bounding box rendering
   * `<span><span>skyvern/forge/sdk/api/llm/config_registry.py</span></span>` - LLM provider configuration
   * `<span><span>skyvern/webeye/browser_factory.py</span></span>` - Browser creation and Chrome profile
   * `<span><span>skyvern/config.py</span></span>` - Main settings file
   * `<span><span>skyvern/forge/agent.py</span></span>` - Main agent class
   * `<span><span>skyvern/forge/sdk/workflow/models/block.py</span></span>` - Workflow block system
1. **Deliverable Created**: I created `<span><span>SKYVERN_ARCHITECTURE_ANALYSIS.md</span></span>` with comprehensive documentation and committed it to the repository.
1. **Second Request**: The user then expressed interest in deploying Skyvern as a production SaaS service:
   * Frontend on Vercel
   * Backend on home WSL
   * Korean-optimized automation platform
1. **Current State**: I was just starting to analyze the frontend/backend architecture for the deployment strategy when the summary was requested.

Let me ensure I capture all technical details accurately:

* Bounding box code location: domUtils.js lines 1907-1918, 2112-2148
* Ollama configuration: config\_registry.py lines 1331-1349
* Chrome profile: browser\_factory.py lines 510-518, 559-590
* GPT-4o-mini: config\_registry.py lines 187-195

Summary:

1. Primary Request and Intent: The user requested a comprehensive code analysis of the Skyvern repository ([https://github.com/Skyvern-AI/skyvern](https://github.com/Skyvern-AI/skyvern)) for a **Naver blog automation project**. The analysis needed to be in Korean and structured as a "Code Map" that developers can immediately use. Key requirements included:
   * Finding the exact code location for **Manus AI-style visual debugging** (Bounding Box + numbered labels on screenshots)
   * **LLM Provider configuration** for Ollama and GPT-4o-mini integration
   * **Chrome profile loading** for captcha bypass
   * Workflow engine analysis
   * Configuration file mapping

   Subsequently, the user expressed intent to deploy Skyvern as a **Korean-optimized automation SaaS platform** with:
   * Frontend deployed on Vercel
   * Backend running on home WSL
1. Key Technical Concepts:
   * **Playwright** - Browser automation framework
   * **LiteLLM** - LLM abstraction layer supporting multiple providers
   * **Bounding Box Rendering** - JavaScript-based visual overlay on web elements
   * **CDP (Chrome DevTools Protocol)** - For connecting to existing Chrome instances
   * **Chrome Profile/User Data Dir** - For session persistence and captcha bypass
   * **Block-based Workflow System** - Modular task execution
   * **FastAPI** - Backend REST API server
   * **Vercel** - Frontend hosting platform
   * **WSL** - Windows Subsystem for Linux for backend hosting
1. Files and Code Sections:
   * **`<span><span>skyvern/webeye/scraper/domUtils.js</span></span>`**
     * Core file for Manus-style visual recognition
     * Contains Bounding Box drawing logic

     ```javascript
     // Lines 1907-1918 - Main drawing function
     function drawBoundingBoxes(elements) {
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

     // Lines 2128-2148 - Box style definition
     boundingBox.style.border = "2px solid blue"; // Customization point
     boundingBox.style.pointerEvents = "none";
     boundingBox.style.zIndex = this.currentZIndex++;
     ```
   * **`<span><span>skyvern/forge/sdk/api/llm/config_registry.py</span></span>`**
     * LLM provider registration and configuration
     * Already implements Ollama and GPT-4o-mini support

     ```python
     # Lines 1331-1349 - Ollama configuration (already implemented!)
     if settings.ENABLE_OLLAMA:
         if settings.OLLAMA_MODEL:
             ollama_model_name = settings.OLLAMA_MODEL
             LLMConfigRegistry.register_config(
                 "OLLAMA",
                 LLMConfig(
                     f"ollama/{ollama_model_name}",
                     ["OLLAMA_SERVER_URL", "OLLAMA_MODEL"],
                     supports_vision=False,  # Note: Vision disabled
                     add_assistant_prefix=False,
                     litellm_params=LiteLLMParams(
                         api_base=settings.OLLAMA_SERVER_URL,
                         api_key=None,
                         api_version=None,
                         model_info={"model_name": f"ollama/{ollama_model_name}"},
                     ),
                 ),
             )

     # Lines 187-195 - GPT-4o-mini (already implemented!)
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
   * **`<span><span>skyvern/webeye/browser_factory.py</span></span>`**
     * Browser creation and Chrome profile management

     ```python
     # Lines 510-518 - Default Chrome profile paths
     def default_user_data_dir() -> pathlib.Path:
         p = platform.system()
         if p == "Darwin":
             return pathlib.Path("~/Library/Application Support/Google/Chrome").expanduser()
         if p == "Windows":
             return pathlib.Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"
         return pathlib.Path("~/.config/google-chrome").expanduser()

     # Lines 471 - Persistent context launch
     browser_context = await playwright.chromium.launch_persistent_context(**browser_args)

     # Lines 586 - CDP connection with user_data_dir
     "--user-data-dir=./tmp/user_data_dir",
     ```
   * **`<span><span>skyvern/config.py</span></span>`**
     * Main settings file with all environment variables

     ```python
     # Lines 300-302 - Ollama settings
     ENABLE_OLLAMA: bool = False
     OLLAMA_SERVER_URL: str | None = None
     OLLAMA_MODEL: str | None = None

     # Lines 146-149 - LLM key settings
     LLM_KEY: str = "OPENAI_GPT4O"
     LLM_API_KEY: str | None = None

     # Lines 34-36 - Browser settings
     BROWSER_TYPE: str = "chromium-headful"
     BROWSER_REMOTE_DEBUGGING_URL: str = "http://127.0.0.1:9222"
     ```
   * **`<span><span>skyvern/forge/agent.py</span></span>`**
     * ForgeAgent main class for task execution
     * Orchestrates browser, LLM, and action handling
   * **`<span><span>skyvern/forge/sdk/workflow/models/block.py</span></span>`**
     * Block-based workflow system (NavigationBlock, ActionBlock, UploadFileBlock, etc.)
   * **`<span><span>SKYVERN_ARCHITECTURE_ANALYSIS.md</span></span>`** (Created)
     * Comprehensive Korean documentation of the entire codebase analysis
     * 525 lines covering all analysis targets
     * Committed to repository
1. Errors and fixes:
   * **File too large error**: `<span><span>domUtils.js</span></span>` exceeded 25000 tokens, used `<span><span>Grep</span></span>` with context instead
   * **Agent directory not found**: `<span><span>skyvern/agent/agent.py</span></span>` doesn't exist; found correct location at `<span><span>skyvern/forge/agent.py</span></span>`
   * No user feedback on errors was given
1. Problem Solving:
   * Successfully located Bounding Box rendering code (was in JavaScript, not Python)
   * Discovered Ollama support is already implemented (just needs `<span><span>supports_vision=True</span></span>` modification)
   * Found GPT-4o-mini is already registered in config\_registry
   * Identified Chrome profile loading via CDP mode as the recommended approach
   * Created comprehensive analysis document in Korean
1. All user messages:
   * **Message 1**: Extensive Korean prompt requesting Skyvern codebase analysis for Naver blog automation with specific analysis targets (visual recognition engine, LLM provider, browser control, workflow engine, config structure) and detailed output format requirements
   * **Message 2**: "네이버 블로그포스팅자동화가 현재 목적이지만 중단기적으로 바로 프로덕션서비스로 진행하고싶어졌어. 한국 최적화된 자동화 플랫폼SaaS로 쓰기에 독보적이야. 어쨋든, 여기 다 오픈소스니까, 프론트는 내 버셀로 띄우고, 백엔드는 우리집 WSL에서 작동하게 하고싶어. 어떻게하면될까? Ultrathink"
1. Pending Tasks:
   * Analyze frontend/backend architecture for Vercel + WSL deployment
   * Create Vercel frontend deployment strategy
   * Set up WSL backend with external exposure (tunneling)
   * Write production architecture design document
   * Korean market optimization considerations
1. Current Work: Immediately before the summary request, I had just:
   1. Completed the comprehensive Skyvern codebase analysis
   1. Created and committed `<span><span>SKYVERN_ARCHITECTURE_ANALYSIS.md</span></span>`
   1. Started responding to the user's second request about deploying as a SaaS platform
   1. Created a TodoWrite with deployment planning tasks

   The user's exact words for the new request: "프론트는 내 버셀로 띄우고, 백엔드는 우리집 WSL에서 작동하게 하고싶어. 어떻게하면될까? Ultrathink"
1. Optional Next Step: Continue analyzing the Skyvern frontend/backend architecture for the **Vercel (frontend) + WSL (backend) deployment** strategy. This directly addresses the user's most recent request.
   Key areas to investigate:
   * `<span><span>skyvern-frontend/</span></span>` directory structure for Vercel deployment
   * API endpoint configuration for cross-origin requests
   * Tunneling solutions (ngrok/Cloudflare Tunnel) for WSL exposure
   * Required environment variables for production deployment

   User's exact quote: "프론트는 내 버셀로 띄우고, 백엔드는 우리집 WSL에서 작동하게 하고싶어". Please continue the conversation from where we left it off without asking the user any further questions. Continue with the last task that you were asked to work on.





Vercel + WSL 배포 아키텍처 분석을 계속 진행하겠습니다. 먼저 프론트엔드 구조를 분석해야 합니다.





**Glob**skyvern-frontend/\*\*/\*.{json,ts,tsx}





**Glob**skyvern-frontend/\*.{json,js,ts}





**Read**/home/user/skyvern/skyvern-frontend/package.json

117줄 읽음







**Read**/home/user/skyvern/skyvern-frontend/vite.config.ts

24줄 읽음







**Read**/home/user/skyvern/skyvern-frontend/src/api/AxiosClient.ts

127줄 읽음







**Grep**apiBaseUrl|VITE\_





**Glob**skyvern-frontend/src/\*\*/env\*.ts





**Glob**skyvern-frontend/.env\*





**Read**/home/user/skyvern/skyvern-frontend/src/util/env.ts

117줄 읽음







**Read**/home/user/skyvern/skyvern-frontend/.env.example

22줄 읽음







이제 백엔드의 CORS 설정과 FastAPI 서버 구성을 확인해야 합니다.





**Grep**CORS|CORSMiddleware|allow\_origins





**Glob**skyvern/forge/\*\*/app\*.py





**Read**/home/user/skyvern/skyvern/forge/api\_app.py

162줄 읽음







**Grep**ALLOWED\_ORIGINS





**Glob**\*\*/docker-compose\*.{yml,yaml}





**Glob**\*\*/Dockerfile\*





**Read**/home/user/skyvern/docker-compose.yml

214줄 읽음







**Read**/home/user/skyvern/Dockerfile

55줄 읽음







**Update Todos**

 현재 프론트엔드/백엔드 아키텍처 분석

 Vercel 배포 전략 수립 중

 WSL 백엔드 설정 및 외부 노출 방안

 프로덕션 아키텍처 설계 문서 작성








이제 프로덕션 배포 아키텍처 문서를 작성하겠습니다.





**Write**/home/user/skyvern/DEPLOYMENT\_ARCHITECTURE.md

`# Skyvern 프로덕션 배포 아키텍처 가이드`



` `



`## 한국형 자동화 SaaS 플랫폼 배포 전략`



` `



`> **목표**: Vercel (프론트엔드) + WSL (백엔드) 구성으로 한국 최적화 자동화 플랫폼 구축`



` `



`---`



` `



`## 1. 아키텍처 개요`



` `



`````

`┌─────────────────────────────────────────────────────────────────────┐`

`│                         사용자 브라우저                               │`

`└─────────────────────────────────────────────────────────────────────┘`

`                                    │`

`                    ┌───────────────┼───────────────┐`

`                    ▼                               ▼`

`    ┌───────────────────────────┐   ┌───────────────────────────────┐`

`    │   Vercel (프론트엔드)       │   │   Cloudflare Tunnel           │`

`    │   - skyvern-frontend       │   │   - api.yourdomain.com        │`

Show full diff (469 more lines)

**Update Todos**

현재 프론트엔드/백엔드 아키텍처 분석

Vercel 프론트엔드 배포 전략 수립

WSL 백엔드 설정 및 외부 노출 방안

프로덕션 아키텍처 설계 문서 작성

vercel.json 파일 생성 중

**Write**/home/user/skyvern/skyvern-frontend/vercel.json

`<span class="token-text">{</span>`

`<span class="token-text">  </span><span class="token-string-special-key">"$schema"</span><span class="token-text">: </span><span class="token-string">"https://openapi.vercel.sh/vercel.json"</span><span class="token-text">,</span>`

`<span class="token-text">  </span><span class="token-string-special-key">"buildCommand"</span><span class="token-text">: </span><span class="token-string">"npm run build"</span><span class="token-text">,</span>`

`<span class="token-text">  </span><span class="token-string-special-key">"outputDirectory"</span><span class="token-text">: </span><span class="token-string">"dist"</span><span class="token-text">,</span>`

`<span class="token-text">  </span><span class="token-string-special-key">"framework"</span><span class="token-text">: </span><span class="token-string">"vite"</span><span class="token-text">,</span>`

`<span class="token-text">  </span><span class="token-string-special-key">"installCommand"</span><span class="token-text">: </span><span class="token-string">"npm install"</span><span class="token-text">,</span>`

`<span class="token-text">  </span><span class="token-string-special-key">"rewrites"</span><span class="token-text">: [</span>`

`<span class="token-text">    {</span>`

`<span class="token-text">      </span><span class="token-string-special-key">"source"</span><span class="token-text">: </span><span class="token-string">"/((?!assets|favicon).*)"</span><span class="token-text">,</span>`

`<span class="token-text">      </span><span class="token-string-special-key">"destination"</span><span class="token-text">: </span><span class="token-string">"/"</span>`

`<span class="token-text">    }</span>`

`<span class="token-text">  ],</span>`

`<span class="token-text">  </span><span class="token-string-special-key">"headers"</span><span class="token-text">: [</span>`

`<span class="token-text">    {</span>`

`<span class="token-text">      </span><span class="token-string-special-key">"source"</span><span class="token-text">: </span><span class="token-string">"/(.*)"</span><span class="token-text">,</span>`

`<span class="token-text">      </span><span class="token-string-special-key">"headers"</span><span class="token-text">: [</span>`

`<span class="token-text">        {</span>`

`<span class="token-text">          </span><span class="token-string-special-key">"key"</span><span class="token-text">: </span><span class="token-string">"X-Content-Type-Options"</span><span class="token-text">,</span>`

`<span class="token-text">          </span><span class="token-string-special-key">"value"</span><span class="token-text">: </span><span class="token-string">"nosniff"</span>`

`<span class="token-text">        },</span>`

Show full diff (26 more lines)

**Write**/home/user/skyvern/skyvern-frontend/.env.production.example

`# Skyvern Frontend - Production Environment Variables`

`# Copy this file to .env.production.local and fill in your values`

`# For Vercel deployment, set these in the Vercel Dashboard → Settings → Environment Variables`

` `

`# ========================`

`# API Configuration`

`# ========================`

`# Replace with your backend URL (Cloudflare Tunnel, ngrok, or custom domain)`

`VITE_API_BASE_URL=https://api.yourdomain.com/api/v1`

` `

`# WebSocket URL for real-time updates (same host as API, use wss:// for HTTPS)`

`VITE_WSS_BASE_URL=wss://api.yourdomain.com/api/v1`

` `

`# Artifact server URL for screenshots and recordings`

`VITE_ARTIFACT_API_BASE_URL=https://artifact.yourdomain.com`

` `

`# ========================`

`# Authentication`

`# ========================`

`# Your API key from Skyvern Settings page`

Show full diff (22 more lines)

**Update Todos**

현재 프론트엔드/백엔드 아키텍처 분석

Vercel 프론트엔드 배포 전략 수립

WSL 백엔드 설정 및 외부 노출 방안

프로덕션 아키텍처 설계 문서 작성

vercel.json 구성 파일 생성

변경사항 커밋 및 푸시 중

**Bash**git status

<details class="font-mono whitespace-pre-wrap [&[open]>summary]:hidden text-text-100"><summary class="cursor-pointer list-none"><span>	skyvern-frontend/vercel.json

nothing added to commit but untracked files present (use &#34;git add&#34; to track)</span><span class="block text-text-500">... +7 라인</span></summary>

</details>

**Bash**git log --oneline -3

14dcc6e Add comprehensive Skyvern codebase analysis report 5b530ca Initial scenario tests infra (#4118) 4dcc8ed Warn 'Authentication failed' (#4117)

**Bash**git add DEPLOYMENT\_ARCHITECTURE.md skyvern-frontend/vercel.json skyvern-frontend/.env.production.example && git commit -m "\$(cat <<'EOF' Add Vercel + WSL deployment architecture guide - DEPLOYMENT\_ARCHITECTURE.md: Comprehensive Korean guide for deploying Skyvern as a SaaS platform with Vercel frontend and WSL backend - vercel.json: Vercel configuration for frontend deployment - .env.production.example: Production environment variables template Covers: Cloudflare Tunnel setup, LLM configuration (GPT-4o-mini/Ollama), Chrome profile for Naver automation, and troubleshooting guide. EOF )"

<details class="font-mono whitespace-pre-wrap [&[open]>summary]:hidden text-text-100"><summary class="cursor-pointer list-none"><span> create mode 100644 DEPLOYMENT_ARCHITECTURE.md
 create mode 100644 skyvern-frontend/.env.production.example
 create mode 100644 skyvern-frontend/vercel.json</span><span class="block text-text-500">... +2 라인</span></summary>

</details>

**Bash**git push -u origin claude/analyze-skyvern-architecture-01NX7mN8NWfWdASX7CUbsFun

branch 'claude/analyze-skyvern-architecture-01NX7mN8NWfWdASX7CUbsFun' set up to track 'origin/claude/analyze-skyvern-architecture-01NX7mN8NWfWdASX7CUbsFun'. To http://127.0.0.1:32880/git/shinjadong/skyvern    14dcc6e..47f746a  claude/analyze-skyvern-architecture-01NX7mN8NWfWdASX7CUbsFun -> claude/analyze-skyvern-architecture-01NX7mN8NWfWdASX7CUbsFun

**Update Todos**

현재 프론트엔드/백엔드 아키텍처 분석

Vercel 프론트엔드 배포 전략 수립

WSL 백엔드 설정 및 외부 노출 방안

프로덕션 아키텍처 설계 문서 작성

vercel.json 구성 파일 생성

변경사항 커밋 및 푸시

배포 아키텍처 문서 작성이 완료되었습니다! 다음 파일들이 생성되어 커밋되었습니다:

## 생성된 파일들


| 파일                                                                 | 설명                      |
| -------------------------------------------------------------------- | ------------------------- |
| `<span><span>DEPLOYMENT_ARCHITECTURE.md</span></span>`               | 전체 배포 가이드 (한국어) |
| `<span><span>skyvern-frontend/vercel.json</span></span>`             | Vercel 배포 설정          |
| `<span><span>skyvern-frontend/.env.production.example</span></span>` | 프로덕션 환경 변수 템플릿 |

---

## 핵심 배포 단계 요약

### 1. WSL 백엔드 시작

```bash
cd skyvern
docker-compose up -d
```

### 2. Cloudflare Tunnel 설정 (외부 노출)

```bash
cloudflared tunnel create skyvern-backend
cloudflared tunnel run skyvern-backend
```

### 3. Vercel 프론트엔드 배포

```bash
cd skyvern-frontend
vercel --prod
```

### 4. Vercel 환경 변수 설정

```
VITE_API_BASE_URL=https://api.yourdomain.com/api/v1
VITE_WSS_BASE_URL=wss://api.yourdomain.com/api/v1
VITE_ARTIFACT_API_BASE_URL=https://artifact.yourdomain.com
```

---

## 네이버 블로그 최적화 팁

* **Chrome 프로필 사용**: `<span><span>BROWSER_TYPE=cdp-connect</span></span>` 설정으로 네이버 로그인 세션 유지
* **GPT-4o-mini 추천**: 저비용으로 한국어 처리 우수 (작업당 약 \$0.001\~\$0.01)
* **Ollama 무료 대안**: `<span><span>qwen2</span><span class="token">.</span><span class="token">5</span><span class="token">:</span><span class="token">7</span><span>b</span><span class="token">-</span><span>instruct</span></span>` 모델이 한국어 지원 좋음

자세한 내용은 `<span><span>DEPLOYMENT_ARCHITECTURE.md</span></span>`를 참조하세요!

**claude/analyze-skyvern-architecture-01NX7mN8NWfWdASX7CUbsFun**

<iframe height="1" width="1"></iframe>

<iframe id="intercom-frame" aria-hidden="true" tabindex="-1" title="Intercom"></iframe>
