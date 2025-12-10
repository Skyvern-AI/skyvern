# 🐉 Skyvern Python 실행 가이드 (권장)

## ✅ 설치 완료!

Skyvern이 성공적으로 실행되었습니다!

---

## 📊 현재 실행 중인 서비스

- **API 서버**: http://localhost:8000
  - Swagger API 문서: http://localhost:8000/docs
- **UI 서버**: http://localhost:8080
- **PostgreSQL**: localhost:5432

---

## 🌐 접속하기

### 1. 브라우저에서 접속

```
http://localhost:8080
```

### 2. API 문서 확인

```
http://localhost:8000/docs
```

---

## 🔑 LLM API 키 설정하기 (중요!)

현재 Skyvern이 실행 중이지만 **LLM API 키가 없으면 작업을 수행할 수 없습니다**.

### 설정 방법

1. `.env` 파일 열기:
```bash
cd /home/tlswk/projects/skyvern
nano .env
```

2. LLM 제공자 활성화 (아래 중 하나 선택):

#### Option 1: OpenAI (추천)
```env
ENABLE_OPENAI=true
OPENAI_API_KEY="your-api-key-here"
LLM_KEY="OPENAI_GPT4O"
```

#### Option 2: Anthropic Claude
```env
ENABLE_ANTHROPIC=true
ANTHROPIC_API_KEY="your-api-key-here"
LLM_KEY="ANTHROPIC_CLAUDE3.5_SONNET"
```

#### Option 3: Google Gemini
```env
ENABLE_GEMINI=true
GEMINI_API_KEY="your-api-key-here"
LLM_KEY="GEMINI_2.5_PRO_PREVIEW"
```

3. 파일 저장 후 Skyvern 재시작:
```bash
cd /home/tlswk/projects/skyvern
./skyvern-restart.sh
```

### 💡 API 키 얻는 방법

- **OpenAI**: https://platform.openai.com/api-keys
- **Anthropic**: https://console.anthropic.com/
- **Google Gemini**: https://aistudio.google.com/app/apikey

---

## 📋 유용한 명령어

### 서비스 관리

```bash
cd /home/tlswk/projects/skyvern

# 서비스 상태 확인
./skyvern-status.sh

# 로그 확인 (실시간)
tail -f skyvern.log

# 서비스 재시작
./skyvern-restart.sh

# 서비스 중지
./skyvern-stop.sh
```

---

## 🛠 문제 해결

### 문제 1: 서비스가 시작되지 않음

**확인사항:**
1. PostgreSQL이 실행 중인지 확인:
```bash
docker ps | grep postgres
```

2. 포트가 사용 가능한지 확인:
```bash
lsof -i :8000  # API 서버
lsof -i :8080  # UI 서버
lsof -i :5432  # PostgreSQL
```

3. 로그 확인:
```bash
tail -100 skyvern.log
```

### 문제 2: PostgreSQL 연결 오류

**해결책:**
```bash
# PostgreSQL 재시작
docker restart skyvern-postgres

# 연결 테스트
docker exec -it skyvern-postgres psql -U skyvern -d skyvern -c "SELECT 1;"
```

### 문제 3: "LLM_KEY is not set" 오류

**해결책:**
`.env` 파일에서 LLM 설정을 확인하고 API 키를 입력한 후 서비스를 재시작하세요.

### 문제 4: 포트 충돌 (EADDRINUSE)

**해결책:**
```bash
# 포트를 사용하는 프로세스 확인
lsof -i :8080

# 프로세스 종료 (PID는 위 명령에서 확인)
kill <PID>

# Skyvern 재시작
./skyvern-restart.sh
```

---

## 🚀 첫 작업 실행하기

1. 브라우저에서 http://localhost:8080 접속
2. "New Task" 또는 "Create Task" 클릭
3. 다음 정보 입력:
   - **URL**: `https://news.ycombinator.com`
   - **Prompt**: `해커뉴스에서 오늘의 인기 게시물 제목을 찾아서 알려줘`
4. "Run Task" 클릭
5. 실시간으로 작업 진행 상황 확인

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

### 최대 실행 단계 조정

```env
# 기본값: 50단계
MAX_STEPS_PER_RUN=50

# 복잡한 작업을 위해 증가
MAX_STEPS_PER_RUN=100
```

### 디버그 모드 활성화

```env
# 로그 레벨 변경
LOG_LEVEL=DEBUG
```

---

## 📦 Python SDK 사용하기

### 설치

```bash
cd /home/tlswk/projects/skyvern
source .venv/bin/activate
```

### 예제 코드

```python
from skyvern import Skyvern

# 로컬 Skyvern 서버 연결
skyvern = Skyvern(
    base_url="http://localhost:8000",
    api_key="YOUR_API_KEY"  # UI의 Settings에서 확인
)

# 작업 실행
task = await skyvern.run_task(
    prompt="네이버에서 '날씨' 검색"
)

print(task)
```

---

## 🔄 자동 시작 설정

시스템 부팅 시 자동으로 Skyvern을 시작하려면:

1. systemd 서비스 파일 생성:
```bash
sudo nano /etc/systemd/system/skyvern.service
```

2. 다음 내용 입력:
```ini
[Unit]
Description=Skyvern Service
After=network.target docker.service

[Service]
Type=simple
User=tlswk
WorkingDirectory=/home/tlswk/projects/skyvern
ExecStart=/home/tlswk/.local/bin/uv run skyvern run all
Restart=always

[Install]
WantedBy=multi-user.target
```

3. 서비스 활성화:
```bash
sudo systemctl enable skyvern
sudo systemctl start skyvern
```

---

## 📚 추가 리소스

- **공식 문서**: https://www.skyvern.com/docs/
- **Discord 커뮤니티**: https://discord.gg/fG2XXEuQX3
- **GitHub**: https://github.com/skyvern-ai/skyvern
- **자세한 가이드**: SKYVERN_실행가이드.md

---

## ⚙️ 시스템 정보

- **Python 버전**: 3.12.3
- **패키지 매니저**: uv
- **데이터베이스**: PostgreSQL 14 (Docker)
- **프로젝트 경로**: `/home/tlswk/projects/skyvern`
- **가상환경**: `.venv/`
- **로그 파일**: `skyvern.log`

---

## 💡 유용한 팁

1. **항상 로그 확인**: 문제가 발생하면 `skyvern.log` 파일을 먼저 확인하세요
2. **Headful 모드 사용**: 디버깅 시 브라우저 화면을 보면 Skyvern이 무엇을 하는지 알 수 있습니다
3. **API 키 비용 주의**: LLM API 사용량에 따라 비용이 발생합니다
4. **정기적인 업데이트**: `uv sync` 명령으로 최신 버전으로 업데이트하세요
5. **데이터 백업**: `postgres-data/` 디렉토리를 정기적으로 백업하세요

---

**작성일**: 2025-11-28  
**버전**: 1.0  
**상태**: ✅ 실행 중

**Happy Automating! 🚀**


