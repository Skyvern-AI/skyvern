# Skyvern 프로젝트 전체 맥락 정리 문서

**작성일**: 2025-11-30
**목적**: 지금까지의 모든 대화 로그와 작업 내역을 종합하여, 누구나 프로젝트의 전체 맥락을 이해할 수 있도록 정리

---

## 1. 프로젝트 개요

### 1.1 최종 목표
**네이버 블로그 자동 포스팅 시스템 구축** → **한국형 웹 자동화 SaaS 플랫폼**으로 발전

### 1.2 핵심 요구사항
| 요구사항 | 설명 | 상태 |
|---------|------|------|
| Chrome 프로필 연동 | 캡차 우회를 위한 기존 로그인 세션 활용 | ✅ 방법 확인 |
| 시각적 디버깅 | Manus AI처럼 Bounding Box + 번호 라벨 표시 | ✅ 코드 위치 확인 |
| 저비용 LLM | GPT-4o-mini 또는 Ollama 연동 | ✅ 이미 구현됨 |
| 로컬 실행 | WSL 환경에서 백엔드 실행 | ✅ 설치 완료 |
| 프로덕션 배포 | Vercel(프론트) + WSL(백엔드) | 📋 계획 수립됨 |

---

## 2. 프로젝트 아키텍처

### 2.1 이원화 전략 (Gemini 세션에서 도출)

```
┌─────────────────────────────────────────────────────────────┐
│                    케어온 자동화 시스템                        │
├─────────────────────┬───────────────────────────────────────┤
│     시흥 (본가)      │           강남 (자취방)                 │
├─────────────────────┼───────────────────────────────────────┤
│ 역할: 콘텐츠 생산    │ 역할: 트래픽/확산                       │
│ 에이전트: Skyvern   │ 에이전트: DroidRun (Phone Farm)        │
│ 네트워크: 고정 IP   │ 네트워크: 유동 IP (테더링)              │
│ 장점: 네이버 신뢰↑  │ 장점: 익명성 최상                       │
└─────────────────────┴───────────────────────────────────────┘
```

**핵심 인사이트**:
- **포스팅(생산)은 PC(안정성)에서** - 고정 IP로 네이버가 신뢰
- **트래픽(소비)은 모바일(유동성)에서** - IP 변경으로 익명성 확보

### 2.2 Skyvern 코드 아키텍처

```
skyvern/
├── cli/                          # CLI 명령어
├── config.py                     # 전체 설정 관리
├── forge/                        # 핵심 비즈니스 로직
│   ├── agent.py                  # ForgeAgent (메인 에이전트)
│   ├── api_app.py                # FastAPI 서버
│   └── sdk/
│       ├── api/llm/              # LLM Provider 추상화
│       │   ├── config_registry.py  # ★ LLM 설정 (Ollama, GPT-4o-mini)
│       │   └── api_handler_factory.py
│       └── workflow/             # 워크플로우 엔진
│           └── models/block.py   # Block 시스템
├── webeye/                       # 브라우저 자동화 엔진
│   ├── browser_factory.py        # ★ Chrome 프로필 로드
│   ├── scraper/
│   │   └── domUtils.js           # ★ Bounding Box 렌더링
│   └── actions/
│       └── handler.py            # 액션 실행 (클릭, 입력)
└── skyvern-frontend/             # React 기반 웹 UI
```

### 2.3 데이터 흐름

```
[사용자 요청]
    ↓
[FastAPI Server] (forge/api_app.py)
    ↓
[ForgeAgent] (forge/agent.py)
    ↓
[Browser Factory] (webeye/browser_factory.py) ←→ [Playwright]
    ↓
[DOM Scraping + Screenshot]
    ↓
[Bounding Box 그리기] (webeye/scraper/domUtils.js)
    ↓
[LLM API Handler] → [GPT-4o-mini / Ollama]
    ↓
[Action 파싱 및 실행] (webeye/actions/handler.py)
    ↓
[결과 반환]
```

---

## 3. 주요 코드 위치 및 수정 포인트

### 3.1 시각적 인식 엔진 (Manus AI 스타일 Bounding Box)

| 항목 | 파일 | 라인 |
|------|------|------|
| 박스 그리기 | `skyvern/webeye/scraper/domUtils.js` | 1907-1918 |
| 박스 스타일 | `skyvern/webeye/scraper/domUtils.js` | 2128-2148 |
| 박스 색상 | `skyvern/webeye/scraper/domUtils.js` | 2139 |

**스타일 커스터마이징 예시**:
```javascript
// 변경 전 (파란색)
boundingBox.style.border = "2px solid blue";

// 변경 후 (Manus AI 스타일 주황색)
boundingBox.style.border = "3px solid #FF6B35";
boundingBox.style.borderRadius = "4px";
boundingBox.style.boxShadow = "0 0 10px rgba(255, 107, 53, 0.5)";
```

### 3.2 LLM Provider 설정

| Provider | 설정 파일 | 라인 | 상태 |
|----------|----------|------|------|
| GPT-4o-mini | `config_registry.py` | 187-195 | ✅ 이미 구현 |
| Ollama | `config_registry.py` | 1331-1349 | ✅ 이미 구현 |
| Ollama Vision | `config_registry.py` | 1337 | ⚠️ `supports_vision=False` 수정 필요 |

**GPT-4o-mini 설정 (.env)**:
```bash
ENABLE_OPENAI=true
OPENAI_API_KEY=sk-xxx
LLM_KEY=OPENAI_GPT4O_MINI
```

**Ollama 설정 (.env)**:
```bash
ENABLE_OLLAMA=true
OLLAMA_SERVER_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2-vision
LLM_KEY=OLLAMA
```

### 3.3 Chrome 프로필 연동 (캡차 우회 핵심)

| 항목 | 파일 | 라인 |
|------|------|------|
| 기본 프로필 경로 | `browser_factory.py` | 510-518 |
| CDP 연결 | `browser_factory.py` | 559-590 |
| Persistent Context | `browser_factory.py` | 453-472 |

**CDP 모드 설정 (추천)**:
```bash
# 1. Chrome 실행 (기존 프로필 사용)
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.config/google-chrome"

# 2. .env 설정
BROWSER_TYPE=chromium-cdp
BROWSER_REMOTE_DEBUGGING_URL=http://127.0.0.1:9222
```

---

## 4. 작업 진행 현황

### 4.1 Claude Code Cloud 세션 (코드베이스 분석)

**완료된 작업**:
1. ✅ Skyvern 전체 아키텍처 분석
2. ✅ Bounding Box 코드 위치 확인 (`domUtils.js`)
3. ✅ LLM Provider 설정 분석 (Ollama, GPT-4o-mini 이미 구현 확인)
4. ✅ Chrome 프로필 연동 방법 파악 (CDP 모드)
5. ✅ 워크플로우 엔진 분석 (Block 시스템)
6. ✅ `SKYVERN_ARCHITECTURE_ANALYSIS.md` 작성 및 커밋

**생성된 결과물**:
- `SKYVERN_ARCHITECTURE_ANALYSIS.md` - 코드베이스 상세 분석
- `DEPLOYMENT_ARCHITECTURE.md` - Vercel + WSL 배포 가이드
- `skyvern-frontend/vercel.json` - Vercel 배포 설정
- `skyvern-frontend/.env.production.example` - 프로덕션 환경 변수 템플릿

### 4.2 Cursor 세션 (로컬 실행 환경 구축)

**완료된 작업**:
1. ✅ Docker Desktop 설정 및 WSL2 통합
2. ✅ PostgreSQL 컨테이너 실행
3. ✅ Python 환경 설치 (uv)
4. ✅ 데이터베이스 마이그레이션 완료
5. ✅ Skyvern 서비스 실행 성공

**현재 서비스 상태**:
| 서비스 | URL | 상태 |
|--------|-----|------|
| Skyvern UI | http://localhost:8080 | ✅ 정상 |
| API 서버 | http://localhost:8000 | ✅ 정상 |
| API 문서 | http://localhost:8000/docs | ✅ 정상 |
| PostgreSQL | localhost:5432 | ✅ 정상 |

**생성된 관리 스크립트**:
- `skyvern-restart.sh` - 서비스 재시작
- `skyvern-stop.sh` - 서비스 중지
- `skyvern-status.sh` - 상태 확인
- `docker-start.sh`, `docker-stop.sh`, `docker-logs.sh`, `docker-status.sh`

**생성된 문서**:
- `README_시작하기.md` - 빠른 시작 가이드
- `PYTHON_실행가이드.md` - Python 환경 상세 가이드
- `SKYVERN_실행가이드.md` - Docker 실행 가이드
- `README.ko.md` - 한국어 README

### 4.3 Gemini 세션 (전략 수립 및 학습 자료)

**주요 논의 내용**:

1. **인터넷 장애 대응 전략**
   - 아이폰 3대를 활용한 테더링 네트워크 구축
   - USB 테더링 vs Wi-Fi 핫스팟 비교
   - 데이터 사용량 관리 전략

2. **이원화 아키텍처 설계**
   - 시흥(PC/Skyvern): 콘텐츠 생산 기지 - 고정 IP
   - 강남(Phone/DroidRun): 트래픽 확산 기지 - 유동 IP

3. **AI 브라우저 비교 분석**
   - **Skyvern (추천)**: Vision 기반, 네이버 스마트에디터에 최적
   - **LaVague**: Text-to-Action, 정형화된 작업에 강점

4. **캡차 우회 전략**
   - **1순위**: Chrome 프로필(User Data Dir) 이식 - 로그인 상태 유지
   - **2순위**: 2Captcha 등 외부 Solver 연동
   - **최후 수단**: Human-in-the-loop (일시 정지 & 재개)

5. **WSL + Windows 프로필 연동**
   - 윈도우 크롬 프로필을 WSL로 복사하여 사용
   - Docker Volume 마운트로 Skyvern에 연결

**생성된 학습 자료**:
- Skyvern 마스터 가이드 (Deep Research 결과)
- NotebookLM용 학습 프롬프트 4종:
  1. 시스템 아키텍처 강의 노트
  2. 네이버 맞춤형 코드 수정 지시서
  3. 네이버 포스팅 YAML 설계
  4. 출퇴근용 팟캐스트 대본

---

## 5. 핵심 기술 요약

### 5.1 Skyvern의 차별점

| 특성 | 기존 도구 (Selenium) | Skyvern |
|------|---------------------|---------|
| 요소 인식 | DOM 파싱 (코드 기반) | Vision AI (이미지 기반) |
| 동적 UI 대응 | 취약 (셀렉터 깨짐) | 강함 (시각적 인식) |
| 네이버 에디터 | 어려움 (복잡한 DOM) | 용이 ("사진 아이콘 클릭") |

### 5.2 캡차 우회의 핵심 원리

```
[문제] 네이버는 새로운 브라우저/환경을 의심 → 캡차 발생

[해결] 기존 로그인된 Chrome 프로필을 Skyvern에 제공
       ↓
       네이버: "평소에 쓰던 브라우저네?" → 캡차 생략
```

**프로필 이식 절차**:
```bash
# 1. WSL에 프로필 폴더 생성
mkdir -p ~/chrome-profile

# 2. 윈도우 프로필 복사 (Chrome 종료 후!)
cp -r /mnt/c/Users/[사용자명]/AppData/Local/Google/Chrome/User\ Data/* ~/chrome-profile/

# 3. docker-compose.yml에 볼륨 마운트 추가
volumes:
  - /home/[사용자]/chrome-profile:/data/chrome-profile
```

### 5.3 워크플로우 Block 시스템

| Block 타입 | 용도 | 네이버 블로그 적용 |
|-----------|------|-------------------|
| `NavigationBlock` | URL 이동 | 블로그 홈 접속 |
| `ActionBlock` | 웹 액션 | 글쓰기 버튼 클릭, 제목/본문 입력 |
| `UploadFileBlock` | 파일 업로드 | 이미지 첨부 |
| `ExtractionBlock` | 데이터 추출 | 발행된 URL 수집 |
| `ValidationBlock` | 결과 검증 | 포스팅 성공 확인 |

---

## 6. 프로덕션 배포 계획

### 6.1 아키텍처

```
┌─────────────────────────────────────────────────────────────────────┐
│                         사용자 브라우저                               │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼                               ▼
    ┌───────────────────────────┐   ┌───────────────────────────────┐
    │   Vercel (프론트엔드)       │   │   Cloudflare Tunnel           │
    │   - skyvern-frontend       │   │   - api.yourdomain.com        │
    │   - React + Vite           │   │   - 무료 HTTPS 제공            │
    └───────────────────────────┘   └───────────────────────────────┘
                                                    │
                                                    ▼
                                    ┌───────────────────────────────┐
                                    │   WSL2 (백엔드)                 │
                                    │   - Skyvern API Server         │
                                    │   - PostgreSQL (Docker)        │
                                    │   - Playwright Browser         │
                                    └───────────────────────────────┘
```

### 6.2 배포 단계

1. **WSL 백엔드 시작**
   ```bash
   cd /home/tlswk/projects/skyvern
   docker-compose up -d  # PostgreSQL
   skyvern run all       # Skyvern 서비스
   ```

2. **Cloudflare Tunnel 설정**
   ```bash
   cloudflared tunnel create skyvern-backend
   cloudflared tunnel run skyvern-backend
   ```

3. **Vercel 프론트엔드 배포**
   ```bash
   cd skyvern-frontend
   vercel --prod
   ```

4. **Vercel 환경 변수 설정**
   ```
   VITE_API_BASE_URL=https://api.yourdomain.com/api/v1
   VITE_WSS_BASE_URL=wss://api.yourdomain.com/api/v1
   ```

---

## 7. 다음 단계 (To-Do)

### 7.1 즉시 필요한 작업
- [ ] LLM API 키 설정 (OpenAI 또는 Ollama)
- [ ] Chrome 프로필 복사 (Windows → WSL)
- [ ] 네이버 로그인 테스트

### 7.2 단기 목표
- [ ] 네이버 블로그 자동 포스팅 워크플로우 작성
- [ ] 이미지 업로드 기능 테스트
- [ ] 에러 처리 및 재시도 로직 구현

### 7.3 중기 목표
- [ ] Vercel + Cloudflare Tunnel 배포
- [ ] 한국형 SaaS 플랫폼 MVP 구축
- [ ] 다중 계정 지원

---

## 8. 참조 파일 목록

### 8.1 문서
| 파일 | 설명 |
|------|------|
| `docs/chat-logs/claudecode-cloud/claudecode-cloud.md` | Claude Code Cloud 대화 로그 |
| `docs/chat-logs/cursor/cursor.md` | Cursor 대화 로그 |
| `docs/chat-logs/gemini/gemini-1.md` | Gemini 대화 로그 |
| `SKYVERN_ARCHITECTURE_ANALYSIS.md` | 코드베이스 분석 보고서 |
| `DEPLOYMENT_ARCHITECTURE.md` | 배포 아키텍처 가이드 |
| `README_시작하기.md` | 빠른 시작 가이드 |
| `PYTHON_실행가이드.md` | Python 환경 가이드 |

### 8.2 설정 파일
| 파일 | 설명 |
|------|------|
| `.env` | 환경 변수 (API 키, DB 설정) |
| `skyvern-frontend/vercel.json` | Vercel 배포 설정 |
| `skyvern-frontend/.env.production.example` | 프로덕션 환경 변수 템플릿 |

### 8.3 관리 스크립트
| 파일 | 설명 |
|------|------|
| `skyvern-restart.sh` | 서비스 재시작 |
| `skyvern-stop.sh` | 서비스 중지 |
| `skyvern-status.sh` | 상태 확인 |
| `docker-start.sh` | Docker 시작 |
| `docker-stop.sh` | Docker 중지 |

---

## 9. 용어 정리

| 용어 | 설명 |
|------|------|
| **Skyvern** | Vision AI 기반 웹 자동화 프레임워크 |
| **CDP** | Chrome DevTools Protocol - 기존 Chrome에 연결하는 방식 |
| **User Data Dir** | Chrome 사용자 프로필 폴더 (쿠키, 세션 포함) |
| **Bounding Box** | 웹 요소 주변에 그리는 시각적 박스 |
| **LiteLLM** | 다중 LLM Provider를 추상화하는 라이브러리 |
| **Block** | Skyvern 워크플로우의 기본 실행 단위 |
| **ForgeAgent** | Skyvern의 메인 에이전트 클래스 |
| **DroidRun** | Android 기기 자동화 도구 (Phone Farm용) |

---

**문서 작성**: Claude Code (Opus 4)
**마지막 업데이트**: 2025-11-30
