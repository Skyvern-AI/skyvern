# 🎉 Skyvern 설치 및 실행 완료!

## ✅ 현재 상태

Skyvern이 성공적으로 설치되고 실행되었습니다!

---

## 🌐 빠른 접속

### 웹 UI 접속
```
http://localhost:8080
```
브라우저에서 위 주소로 접속하여 Skyvern을 사용할 수 있습니다.

### API 문서
```
http://localhost:8000/docs
```
Swagger UI에서 API 문서를 확인하고 테스트할 수 있습니다.

---

## ⚠️ 중요: LLM API 키 설정 필요!

**Skyvern이 실행 중이지만, 실제 작업을 수행하려면 LLM API 키가 필요합니다.**

### 빠른 설정 방법

1. `.env` 파일 열기:
```bash
nano /home/tlswk/projects/skyvern/.env
```

2. 다음 항목 수정 (OpenAI 예시):
```env
ENABLE_OPENAI=true
OPENAI_API_KEY="sk-your-actual-api-key-here"
LLM_KEY="OPENAI_GPT4O"
```

3. 저장 후 재시작:
```bash
cd /home/tlswk/projects/skyvern
./skyvern-restart.sh
```

### API 키 얻기

- **OpenAI**: https://platform.openai.com/api-keys (추천)
- **Anthropic**: https://console.anthropic.com/
- **Google Gemini**: https://aistudio.google.com/app/apikey

---

## 📋 유용한 명령어

모든 명령어는 프로젝트 디렉토리에서 실행하세요:
```bash
cd /home/tlswk/projects/skyvern
```

### 서비스 관리

```bash
# 상태 확인
./skyvern-status.sh

# 로그 확인 (실시간)
tail -f skyvern.log

# 서비스 재시작
./skyvern-restart.sh

# 서비스 중지
./skyvern-stop.sh
```

---

## 📖 자세한 가이드

이 프로젝트 폴더에 다음 가이드 문서들이 있습니다:

- **PYTHON_실행가이드.md** - Python 환경 실행 가이드 (현재 방식, 권장)
- **SKYVERN_실행가이드.md** - Docker 실행 가이드
- **README.md** - 프로젝트 개요 및 기능 설명

---

## 🚀 첫 작업 시작하기

1. 브라우저에서 http://localhost:8080 접속
2. **LLM API 키를 먼저 설정하세요!** (위 안내 참조)
3. "New Task" 클릭
4. 예제 작업:
   - URL: `https://news.ycombinator.com`
   - Prompt: `오늘의 인기 게시물 제목 5개를 찾아줘`
5. "Run Task" 클릭하고 결과 확인

---

## 🛠 문제 해결

### 서비스가 응답하지 않을 때
```bash
# 1. 서비스 상태 확인
./skyvern-status.sh

# 2. 로그 확인
tail -100 skyvern.log

# 3. 재시작
./skyvern-restart.sh
```

### PostgreSQL 문제
```bash
# PostgreSQL 재시작
docker restart skyvern-postgres

# 연결 테스트
docker exec -it skyvern-postgres psql -U skyvern -d skyvern -c "SELECT 1;"
```

---

## 💡 도움말

- **Discord**: https://discord.gg/fG2XXEuQX3
- **공식 문서**: https://www.skyvern.com/docs/
- **GitHub**: https://github.com/skyvern-ai/skyvern

---

## 📁 프로젝트 구조

```
/home/tlswk/projects/skyvern/
├── .env                     # 환경 설정 파일
├── .venv/                   # Python 가상환경
├── skyvern.log              # 서비스 로그
├── postgres-data/           # 데이터베이스 데이터
├── artifacts/               # 작업 결과물
├── videos/                  # 실행 화면 녹화
├── skyvern-status.sh        # 상태 확인 스크립트
├── skyvern-restart.sh       # 재시작 스크립트
├── skyvern-stop.sh          # 중지 스크립트
├── PYTHON_실행가이드.md     # 자세한 가이드
└── README_시작하기.md       # 이 파일
```

---

**설치 완료 일시**: 2025-11-28  
**실행 방식**: Python (uv) + PostgreSQL (Docker)  
**상태**: ✅ 실행 중

**축하합니다! Skyvern을 사용할 준비가 되었습니다! 🎉**

다음 단계: LLM API 키를 설정하고 첫 작업을 시작하세요!

