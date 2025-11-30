# 🐉 Skyvern Docker 실행 가이드

## 📋 목차
1. [환경 준비 완료 사항](#환경-준비-완료-사항)
2. [LLM API 키 설정하기](#llm-api-키-설정하기)
3. [Skyvern 실행하기](#skyvern-실행하기)
4. [접속 및 사용법](#접속-및-사용법)
5. [문제 해결](#문제-해결)

---

## ✅ 환경 준비 완료 사항

현재 시스템에 다음 항목들이 설정되어 있습니다:

- ✅ **Docker Desktop** 설치 및 실행 중
- ✅ **WSL2 Docker Alias** 설정 완료
- ✅ **필수 디렉토리** 생성 완료
  - `artifacts/` - 작업 결과물 저장
  - `videos/` - 실행 화면 녹화
  - `har/` - HTTP 아카이브 파일
  - `log/` - 로그 파일
  - `postgres-data/` - 데이터베이스 데이터
  - `.streamlit/` - Streamlit 설정

---

## 🔑 LLM API 키 설정하기

Skyvern은 LLM(Large Language Model)을 사용하여 웹 자동화를 수행합니다.
**반드시 하나 이상의 LLM 제공자 API 키를 설정해야 합니다.**

### 지원하는 LLM 제공자

현재 프로젝트 폴더: `/home/tlswk/projects/skyvern`

#### Option 1: OpenAI (추천)

`.env` 파일을 수정합니다:

```bash
nano .env
```

다음 항목들을 수정:

```env
# OpenAI 활성화
ENABLE_OPENAI=true
OPENAI_API_KEY="your-openai-api-key-here"

# 사용할 모델 선택
LLM_KEY="OPENAI_GPT4O"
```

**추천 모델:**
- `OPENAI_GPT4O` - 최신 GPT-4o 모델 (추천)
- `OPENAI_GPT4O_MINI` - 저렴한 GPT-4o Mini
- `OPENAI_O4_MINI` - O4 Mini 모델

#### Option 2: Anthropic Claude (추천)

```env
# Anthropic 활성화
ENABLE_ANTHROPIC=true
ANTHROPIC_API_KEY="your-anthropic-api-key-here"

# 사용할 모델 선택
LLM_KEY="ANTHROPIC_CLAUDE3.5_SONNET"
```

**추천 모델:**
- `ANTHROPIC_CLAUDE3.5_SONNET` - Claude 3.5 Sonnet
- `ANTHROPIC_CLAUDE3.7_SONNET` - Claude 3.7 Sonnet
- `ANTHROPIC_CLAUDE4_SONNET` - Claude 4 Sonnet

#### Option 3: Google Gemini

```env
# Gemini 활성화
ENABLE_GEMINI=true
GEMINI_API_KEY="your-gemini-api-key-here"

# 사용할 모델 선택
LLM_KEY="GEMINI_2.5_PRO_PREVIEW"
```

#### Option 4: Azure OpenAI

```env
# Azure 활성화
ENABLE_AZURE=true
LLM_KEY="AZURE_OPENAI"
AZURE_DEPLOYMENT="your-deployment-name"
AZURE_API_KEY="your-azure-api-key"
AZURE_API_BASE="https://your-resource.openai.azure.com/"
AZURE_API_VERSION="2024-02-01"
```

### 💡 **API 키를 얻는 방법**

- **OpenAI**: https://platform.openai.com/api-keys
- **Anthropic**: https://console.anthropic.com/
- **Google Gemini**: https://aistudio.google.com/app/apikey
- **Azure**: Azure Portal > OpenAI 리소스

---

## 🚀 Skyvern 실행하기

### 1. Docker Compose로 실행

```bash
cd /home/tlswk/projects/skyvern

# 백그라운드에서 실행
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe up -d
```

### 2. 실행 로그 확인

```bash
# 전체 로그 확인
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe logs -f

# Skyvern 서비스만 확인
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe logs -f skyvern

# UI 서비스만 확인
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe logs -f skyvern-ui
```

### 3. 실행 상태 확인

```bash
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe ps
```

**정상 실행 시 다음과 같이 표시됩니다:**
```
NAME                IMAGE                               STATUS
postgres            postgres:14-alpine                  Up (healthy)
skyvern             public.ecr.aws/skyvern/skyvern     Up (healthy)
skyvern-ui          public.ecr.aws/skyvern/skyvern-ui  Up
```

---

## 🌐 접속 및 사용법

### 웹 UI 접속

서비스가 정상적으로 시작되면 브라우저에서 접속합니다:

- **Skyvern UI**: http://localhost:8080
- **API 서버**: http://localhost:8000
- **API 문서 (Swagger)**: http://localhost:8000/docs

### 첫 작업 실행하기

1. 브라우저에서 http://localhost:8080 접속
2. 새 Task 생성
3. 다음 정보 입력:
   - **URL**: 자동화하려는 웹사이트 주소
   - **Prompt**: 수행할 작업 설명 (예: "네이버에서 '날씨' 검색")
4. "Run Task" 클릭
5. 실시간으로 작업 진행 상황 확인

### 예제 작업

```python
# Python SDK 사용 예제
from skyvern import Skyvern

skyvern = Skyvern(
    base_url="http://localhost:8000",
    api_key="YOUR_API_KEY"  # UI의 Settings에서 확인
)

task = await skyvern.run_task(
    prompt="해커뉴스에서 오늘의 인기 게시물 찾기"
)
print(task)
```

---

## 🛠 문제 해결

### 문제 1: Docker Compose 명령이 작동하지 않음

**해결책:**

```bash
# 전체 경로로 실행
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe --version

# 또는 alias 재설정
source ~/.bashrc
```

### 문제 2: "LLM_KEY is not set" 오류

**원인:** `.env` 파일에 LLM API 키가 설정되지 않음

**해결책:**
1. `.env` 파일 열기: `nano .env`
2. LLM 제공자 활성화 및 API 키 입력
3. 컨테이너 재시작: 
   ```bash
   /mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe restart skyvern
   ```

### 문제 3: 포트가 이미 사용 중

**에러:** "port is already allocated"

**해결책:**
```bash
# 실행 중인 컨테이너 확인
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker.exe ps -a

# 기존 컨테이너 중지 및 제거
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe down

# 다시 시작
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe up -d
```

### 문제 4: 데이터베이스 연결 오류

**해결책:**
```bash
# Postgres 컨테이너 로그 확인
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe logs postgres

# 데이터베이스 재시작
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe restart postgres

# 30초 대기 후 Skyvern 재시작
sleep 30
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe restart skyvern
```

### 문제 5: Docker Desktop이 실행되지 않음

**해결책:**
```bash
# Windows에서 Docker Desktop 시작
powershell.exe -Command "Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'"

# 30초 대기
sleep 30

# Docker 상태 확인
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker.exe ps
```

---

## 📊 유용한 명령어

### 서비스 관리

```bash
# 서비스 시작
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe up -d

# 서비스 중지
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe stop

# 서비스 중지 및 제거 (데이터는 보존됨)
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe down

# 서비스 재시작
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe restart

# 모든 것 제거 (데이터 포함, 주의!)
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe down -v
```

### 로그 관리

```bash
# 실시간 로그 보기
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe logs -f

# 마지막 100줄만 보기
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe logs --tail=100

# 특정 시간 이후 로그
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe logs --since 30m
```

### 컨테이너 접속

```bash
# Skyvern 컨테이너 내부 쉘 접속
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe exec skyvern bash

# Postgres 데이터베이스 접속
/mnt/c/Program\ Files/Docker/Docker/resources/bin/docker-compose.exe exec postgres psql -U skyvern -d skyvern
```

---

## 🔧 고급 설정

### 브라우저 모드 변경

`.env` 파일에서:

```env
# Headless 모드 (화면 없이 실행, 빠름)
BROWSER_TYPE="chromium-headless"

# Headful 모드 (브라우저 화면 보임, 디버깅에 유용)
BROWSER_TYPE="chromium-headful"
```

### 최대 실행 단계 설정

```env
# 기본값: 50단계
MAX_STEPS_PER_RUN=50

# 복잡한 작업을 위해 증가
MAX_STEPS_PER_RUN=100
```

### 디버그 모드

```env
# 로그 레벨 변경
LOG_LEVEL=DEBUG
```

---

## 📚 추가 리소스

- **공식 문서**: https://www.skyvern.com/docs/
- **Discord 커뮤니티**: https://discord.gg/fG2XXEuQX3
- **GitHub**: https://github.com/skyvern-ai/skyvern
- **데모 비디오**: README.md 참조

---

## ⚙️ 시스템 요구사항

- **RAM**: 최소 4GB, 권장 8GB 이상
- **디스크**: 최소 10GB 여유 공간
- **Docker Desktop**: 최신 버전
- **WSL2**: Ubuntu 20.04 이상

---

## 🎯 빠른 시작 체크리스트

- [ ] Docker Desktop 실행 확인
- [ ] `.env` 파일에 LLM API 키 설정
- [ ] `docker-compose up -d` 실행
- [ ] http://localhost:8080 접속 확인
- [ ] 첫 Task 실행

---

**작성일**: 2025-11-28  
**버전**: 1.0  
**문의**: Skyvern Discord 또는 GitHub Issues

---

## 💡 팁

1. **API 키는 반드시 설정하세요** - LLM 없이는 Skyvern이 작동하지 않습니다
2. **로그를 자주 확인하세요** - 문제 발생 시 로그에서 원인을 찾을 수 있습니다
3. **첫 실행은 느릴 수 있습니다** - Docker 이미지 다운로드에 시간이 걸립니다
4. **비용 주의** - LLM API 사용량에 따라 비용이 발생합니다
5. **Headful 모드 사용** - 디버깅 시 브라우저 화면을 보면 도움됩니다

---

**Happy Automating! 🚀**


