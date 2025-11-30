# Skyvern 프로덕션 배포 아키텍처 가이드

## 한국형 자동화 SaaS 플랫폼 배포 전략

> **목표**: Vercel (프론트엔드) + WSL (백엔드) 구성으로 한국 최적화 자동화 플랫폼 구축

---

## 1. 아키텍처 개요

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
    │   - your-domain.vercel.app │   │   - wss.yourdomain.com        │
    └───────────────────────────┘   └───────────────────────────────┘
                                                    │
                                    ┌───────────────┴───────────────┐
                                    ▼                               ▼
                    ┌───────────────────────────┐   ┌───────────────────────────┐
                    │   WSL Backend (Docker)     │   │   PostgreSQL Database     │
                    │   - FastAPI Server :8000   │   │   - Port 5432             │
                    │   - WebSocket :8000        │   │                           │
                    │   - Artifact Server :9090  │   │                           │
                    └───────────────────────────┘   └───────────────────────────┘
```

---

## 2. 프론트엔드: Vercel 배포

### 2.1 Vercel 프로젝트 설정

#### 방법 1: GitHub 연동 자동 배포 (추천)

```bash
# 1. skyvern-frontend만 별도 레포로 분리하거나 monorepo 설정

# 2. Vercel에서 Import 시 설정:
#    - Root Directory: skyvern-frontend
#    - Framework Preset: Vite
#    - Build Command: npm run build
#    - Output Directory: dist
```

#### 방법 2: Vercel CLI 수동 배포

```bash
# Vercel CLI 설치
npm i -g vercel

# 프론트엔드 디렉토리로 이동
cd skyvern-frontend

# 프로덕션 배포
vercel --prod
```

### 2.2 Vercel 환경 변수 설정

Vercel Dashboard → Settings → Environment Variables:

```env
# 필수 환경 변수
VITE_API_BASE_URL=https://api.yourdomain.com/api/v1
VITE_WSS_BASE_URL=wss://api.yourdomain.com/api/v1
VITE_ARTIFACT_API_BASE_URL=https://artifact.yourdomain.com
VITE_SKYVERN_API_KEY=your-api-key-from-settings

# 선택적 환경 변수
VITE_ENVIRONMENT=production
VITE_ENABLE_LOG_ARTIFACTS=false
VITE_ENABLE_CODE_BLOCK=true
```

### 2.3 vercel.json 설정 파일

`skyvern-frontend/vercel.json` 생성:

```json
{
  "buildCommand": "npm run build",
  "outputDirectory": "dist",
  "framework": "vite",
  "rewrites": [
    { "source": "/(.*)", "destination": "/" }
  ],
  "headers": [
    {
      "source": "/(.*)",
      "headers": [
        { "key": "X-Content-Type-Options", "value": "nosniff" },
        { "key": "X-Frame-Options", "value": "DENY" },
        { "key": "X-XSS-Protection", "value": "1; mode=block" }
      ]
    }
  ]
}
```

---

## 3. 백엔드: WSL 설정

### 3.1 WSL 환경 준비

```bash
# WSL 버전 확인 (WSL2 권장)
wsl --version

# Docker Desktop 설치 후 WSL2 통합 활성화
# Settings → Resources → WSL Integration → Enable for your distro
```

### 3.2 백엔드 서비스 실행

```bash
# 프로젝트 클론
git clone https://github.com/Skyvern-AI/skyvern.git
cd skyvern

# .env 파일 생성
cp .env.example .env

# LLM 설정 (Ollama 또는 GPT-4o-mini)
# 아래 섹션 4 참조

# Docker Compose로 실행
docker-compose up -d
```

### 3.3 .env 파일 구성

```env
# ========================
# 데이터베이스 설정
# ========================
DATABASE_STRING=postgresql+psycopg://skyvern:skyvern@postgres:5432/skyvern

# ========================
# 브라우저 설정
# ========================
BROWSER_TYPE=chromium-headful
# CDP 모드 (Chrome 프로필 사용 시)
# BROWSER_TYPE=cdp-connect
# BROWSER_REMOTE_DEBUGGING_URL=http://host.docker.internal:9222/

# ========================
# CORS 설정 (Vercel 도메인 허용)
# ========================
ALLOWED_ORIGINS=["https://your-app.vercel.app", "https://yourdomain.com", "*"]

# ========================
# LLM 설정 (아래 옵션 중 선택)
# ========================

# 옵션 1: GPT-4o-mini (저비용 추천)
ENABLE_OPENAI=true
LLM_KEY=OPENAI_GPT4O_MINI
OPENAI_API_KEY=sk-your-openai-key

# 옵션 2: Ollama (로컬 무료)
# ENABLE_OLLAMA=true
# LLM_KEY=OLLAMA
# OLLAMA_MODEL=qwen2.5:7b-instruct
# OLLAMA_SERVER_URL=http://host.docker.internal:11434

# ========================
# 보안 설정
# ========================
ENV=production
SECRET_KEY=your-secure-secret-key-here
```

---

## 4. 외부 노출: Cloudflare Tunnel (권장)

### 4.1 Cloudflare Tunnel이 최적인 이유

| 방식 | 장점 | 단점 |
|------|------|------|
| **Cloudflare Tunnel** | 무료, HTTPS 자동, DDoS 보호 | Cloudflare 계정 필요 |
| ngrok | 설정 간단 | 무료 제한, 도메인 변경 |
| Port Forwarding | 직접 제어 | 고정 IP 필요, 보안 위험 |

### 4.2 Cloudflare Tunnel 설정

```bash
# 1. cloudflared 설치 (WSL 내부)
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cloudflared.deb
sudo dpkg -i cloudflared.deb

# 2. Cloudflare 로그인
cloudflared tunnel login

# 3. 터널 생성
cloudflared tunnel create skyvern-backend

# 4. 터널 ID 확인
cloudflared tunnel list
```

### 4.3 config.yml 설정

`~/.cloudflared/config.yml`:

```yaml
tunnel: <your-tunnel-id>
credentials-file: /home/<user>/.cloudflared/<tunnel-id>.json

ingress:
  # API 엔드포인트
  - hostname: api.yourdomain.com
    service: http://localhost:8000

  # WebSocket 지원 (같은 포트)
  - hostname: wss.yourdomain.com
    service: http://localhost:8000
    originRequest:
      noTLSVerify: true

  # Artifact 서버
  - hostname: artifact.yourdomain.com
    service: http://localhost:9090

  # Catch-all
  - service: http_status:404
```

### 4.4 DNS 설정

```bash
# Cloudflare DNS에 CNAME 레코드 추가
cloudflared tunnel route dns skyvern-backend api.yourdomain.com
cloudflared tunnel route dns skyvern-backend artifact.yourdomain.com
```

### 4.5 터널 실행

```bash
# 포그라운드 실행 (테스트)
cloudflared tunnel run skyvern-backend

# 백그라운드 서비스로 설치 (프로덕션)
sudo cloudflared service install
sudo systemctl start cloudflared
sudo systemctl enable cloudflared
```

---

## 5. 대안: ngrok 사용 시

### 5.1 ngrok 설정

```bash
# ngrok 설치
curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list
sudo apt update && sudo apt install ngrok

# 인증
ngrok config add-authtoken YOUR_NGROK_TOKEN

# 실행 (여러 포트)
ngrok start --all --config ngrok.yml
```

### 5.2 ngrok.yml 설정

```yaml
version: "2"
authtoken: YOUR_NGROK_TOKEN
tunnels:
  api:
    addr: 8000
    proto: http
    hostname: api.yourdomain.ngrok.io  # 유료 플랜 필요
  artifact:
    addr: 9090
    proto: http
```

---

## 6. LLM 설정 상세

### 6.1 GPT-4o-mini 설정 (저비용 추천)

```env
# .env 파일
ENABLE_OPENAI=true
LLM_KEY=OPENAI_GPT4O_MINI
OPENAI_API_KEY=sk-your-openai-key

# 비용 절감 옵션
SECONDARY_LLM_KEY=OPENAI_GPT4O_MINI  # 보조 작업도 저비용 모델
```

**예상 비용**: 작업당 약 $0.001 ~ $0.01

### 6.2 Ollama 설정 (무료)

```bash
# WSL에서 Ollama 설치
curl -fsSL https://ollama.com/install.sh | sh

# 모델 다운로드 (한국어 지원 좋은 모델)
ollama pull qwen2.5:7b-instruct
ollama pull llama3.2:3b  # 경량 대안

# Ollama 서버 시작
ollama serve
```

```env
# .env 파일
ENABLE_OLLAMA=true
LLM_KEY=OLLAMA
OLLAMA_MODEL=qwen2.5:7b-instruct
OLLAMA_SERVER_URL=http://host.docker.internal:11434
```

**주의**: Ollama는 현재 `supports_vision=False`로 설정됨. 스크린샷 분석에 제한이 있음.

### 6.3 비전 모델 활성화 (Ollama)

Ollama에서 비전을 사용하려면 `config_registry.py` 수정 필요:

```python
# skyvern/forge/sdk/api/llm/config_registry.py 라인 1340 수정
supports_vision=True,  # False → True
```

그리고 비전 지원 모델 사용:

```bash
ollama pull llava:7b  # 비전 지원 모델
```

---

## 7. 네이버 블로그 자동화 최적화

### 7.1 Chrome 프로필 설정 (캡차 우회)

```bash
# Windows에서 Chrome 프로필로 실행
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="C:\chrome-skyvern-profile" ^
  --no-first-run ^
  --no-default-browser-check

# 이 Chrome으로 네이버에 미리 로그인해두면 세션 유지됨
```

```env
# .env 파일
BROWSER_TYPE=cdp-connect
BROWSER_REMOTE_DEBUGGING_URL=http://host.docker.internal:9222/
```

### 7.2 한국어 최적화 프롬프트 팁

네이버 블로그 작업 시 프롬프트 예시:

```
Navigate to Naver Blog Editor and create a new post.
The blog should have:
- Title: {title}
- Content: {content}
- Tags: {tags}

Important Korean web considerations:
- Wait for page fully loaded (네이버 is often slow)
- Look for Korean buttons like "발행", "저장", "글쓰기"
- Handle any popup modals by clicking "확인" or "닫기"
```

---

## 8. 프로덕션 체크리스트

### 8.1 보안

- [ ] `SECRET_KEY`를 강력한 랜덤 문자열로 설정
- [ ] `ALLOWED_ORIGINS`에 실제 프론트엔드 도메인만 허용
- [ ] API 키를 환경 변수로 관리 (코드에 하드코딩 금지)
- [ ] HTTPS 강제 (Cloudflare Tunnel 사용 시 자동)
- [ ] Rate limiting 설정 고려

### 8.2 모니터링

```bash
# Docker 로그 확인
docker-compose logs -f skyvern

# 시스템 리소스 모니터링
htop
docker stats
```

### 8.3 백업

```bash
# PostgreSQL 백업
docker-compose exec postgres pg_dump -U skyvern skyvern > backup.sql

# Artifacts 백업
tar -czvf artifacts_backup.tar.gz ./artifacts
```

---

## 9. 트러블슈팅

### 9.1 WebSocket 연결 실패

```
Error: WebSocket connection failed
```

**해결**:
1. `VITE_WSS_BASE_URL`이 올바른지 확인
2. Cloudflare Tunnel에서 WebSocket 지원 확인
3. 브라우저 개발자 도구 Network 탭에서 WS 연결 확인

### 9.2 CORS 오류

```
Access-Control-Allow-Origin header missing
```

**해결**:
1. `.env`의 `ALLOWED_ORIGINS`에 Vercel 도메인 추가
2. Docker 컨테이너 재시작: `docker-compose restart skyvern`

### 9.3 Artifact 로드 실패

```
Failed to load artifact
```

**해결**:
1. Artifact 서버 (9090) 터널 설정 확인
2. `VITE_ARTIFACT_API_BASE_URL` 확인
3. 볼륨 마운트 확인: `docker-compose exec skyvern ls /data/artifacts`

---

## 10. 배포 명령어 요약

```bash
# === 백엔드 (WSL) ===
cd skyvern
docker-compose up -d
cloudflared tunnel run skyvern-backend

# === 프론트엔드 (로컬에서 Vercel로) ===
cd skyvern-frontend
vercel --prod

# === 상태 확인 ===
curl https://api.yourdomain.com/api/v1/health
```

---

## 11. 다음 단계

1. **커스텀 워크플로우 개발**: 네이버 블로그 전용 워크플로우 블록 추가
2. **다국어 지원**: 한국어 UI 번역
3. **스케줄링**: 정기 포스팅을 위한 크론 작업 설정
4. **분석 대시보드**: 자동화 작업 통계 시각화

---

**작성일**: 2025-11-30
**버전**: 1.0
**참조**: [Skyvern GitHub](https://github.com/Skyvern-AI/skyvern)
