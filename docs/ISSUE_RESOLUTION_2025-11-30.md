# 🔧 이슈 해결 보고서

**작성일**: 2025-11-30 15:30 KST
**작성자**: Claude Code (Cloud Architect)
**수신**: Cursor (Local Builder)
**상태**: 🟡 **해결 방안 제시**

---

## 📋 이슈 분석

### 문제 요약
- Cloudflare Quick Tunnel URL 생성됨
- 터널 연결 등록 성공 (connIndex=0, location=icn06)
- **하지만 터널 URL → 404 Not Found**

### 근본 원인 분석

**WSL2 네트워킹 특성**:
```
Windows Host ←→ WSL2 (별도 가상 네트워크)
                   ↓
              localhost:8000 (Skyvern)
                   ↓
              cloudflared (터널)
```

**문제점**: cloudflared가 WSL2 내부에서 실행되지만, Quick Tunnel은 때때로 Windows 호스트의 네트워크를 통해 연결을 시도합니다. WSL2의 가상화된 네트워크 특성상 `localhost`가 혼란을 일으킬 수 있습니다.

---

## ✅ 해결 방안

### 방안 1: ngrok 사용 (가장 간단, 추천)

```bash
# 1. ngrok 설치
curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | \
  sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null && \
  echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | \
  sudo tee /etc/apt/sources.list.d/ngrok.list && \
  sudo apt update && sudo apt install ngrok

# 2. ngrok 계정 인증 (https://ngrok.com에서 무료 가입)
ngrok config add-authtoken YOUR_NGROK_AUTH_TOKEN

# 3. 터널 실행
ngrok http 8000

# 4. 생성된 URL 확인 (예: https://abc123.ngrok-free.app)
```

**장점**:
- WSL2 호환성 우수
- 무료 플랜으로 충분
- 간단한 설정

**Vercel 환경변수 업데이트**:
```env
VITE_API_BASE_URL=https://abc123.ngrok-free.app/api/v1
VITE_WSS_BASE_URL=wss://abc123.ngrok-free.app/api/v1
```

---

### 방안 2: Cloudflare Tunnel 수정 (WSL2 IP 사용)

WSL2의 실제 IP 주소를 사용하여 터널 연결:

```bash
# 1. WSL2 IP 확인
ip addr show eth0 | grep -oP '(?<=inet\s)\d+(\.\d+){3}'
# 예: 172.25.176.1

# 2. 해당 IP로 터널 실행
cloudflared tunnel --url http://172.25.176.1:8000

# 또는 0.0.0.0으로 직접 지정
cloudflared tunnel --url http://0.0.0.0:8000 --http2-origin
```

---

### 방안 3: Cloudflare Named Tunnel (안정적, 영구적)

```bash
# 1. Cloudflare 로그인
cloudflared login

# 2. Named Tunnel 생성
cloudflared tunnel create skyvern-backend

# 3. 설정 파일 생성
cat > ~/.cloudflared/config.yml << 'EOF'
tunnel: <TUNNEL_ID>
credentials-file: /home/$USER/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: api.yourdomain.com
    service: http://localhost:8000
    originRequest:
      noTLSVerify: true
      http2Origin: true
  - service: http_status:404
EOF

# 4. DNS 라우팅
cloudflared tunnel route dns skyvern-backend api.yourdomain.com

# 5. 터널 실행
cloudflared tunnel run skyvern-backend
```

---

### 방안 4: 완전 클라우드 배포 (Railway)

백엔드도 클라우드에 배포하면 터널 문제 완전 해결:

```bash
# 1. Railway CLI 설치
npm i -g @railway/cli

# 2. 로그인
railway login

# 3. 프로젝트 생성 및 배포
railway init
railway up

# 4. 환경변수 설정
railway variables set DATABASE_STRING="..."
railway variables set OPENAI_API_KEY="..."
```

**Railway 무료 플랜**: 월 $5 크레딧 (충분함)

---

## 🔧 즉시 적용 가능한 해결책

### ngrok 스크립트 생성

```bash
#!/bin/bash
# skyvern-ngrok.sh

# Skyvern 서버 실행 확인
if ! curl -s http://localhost:8000/docs > /dev/null; then
    echo "❌ Skyvern 서버가 실행 중이 아닙니다!"
    echo "먼저 실행: ./skyvern-restart.sh"
    exit 1
fi

echo "🚀 ngrok 터널 시작..."
ngrok http 8000 --log=stdout
```

### 환경변수 자동 업데이트 스크립트

```bash
#!/bin/bash
# update-vercel-env.sh

NGROK_URL=$(curl -s http://localhost:4040/api/tunnels | jq -r '.tunnels[0].public_url')

if [ -z "$NGROK_URL" ]; then
    echo "❌ ngrok이 실행 중이 아닙니다!"
    exit 1
fi

echo "📝 ngrok URL: $NGROK_URL"
echo ""
echo "Vercel 환경변수로 설정하세요:"
echo "VITE_API_BASE_URL=${NGROK_URL}/api/v1"
echo "VITE_WSS_BASE_URL=$(echo $NGROK_URL | sed 's/https/wss/')/api/v1"
```

---

## 📊 해결 방안 비교

| 방안 | 난이도 | 안정성 | 비용 | 영구 URL |
|------|--------|--------|------|----------|
| ngrok | ⭐ 쉬움 | 🟢 높음 | 무료 | ❌ (유료시 가능) |
| CF Named Tunnel | ⭐⭐ 중간 | 🟢 높음 | 무료 | ✅ |
| Railway | ⭐⭐ 중간 | 🟢 높음 | $5/월 | ✅ |
| WSL2 IP 수정 | ⭐ 쉬움 | 🟡 중간 | 무료 | ❌ |

---

## 🎯 권장 순서

1. **즉시**: ngrok 시도 (5분 소요)
2. **단기**: Cloudflare Named Tunnel 설정 (30분 소요)
3. **장기**: Railway 또는 Render에 백엔드 배포

---

## 📝 추가 수정 사항

### Ollama Vision 지원 활성화

`config_registry.py`에서 Ollama Vision 지원이 비활성화되어 있었습니다. 이를 수정했습니다:

```python
# 변경 전 (라인 1340)
supports_vision=False,  # Ollama does not support vision yet

# 변경 후
supports_vision=True,  # Ollama supports vision models (llava, qwen2-vl, etc.)
```

이제 Ollama Vision 모델 (llava, qwen2-vl, llama3.2-vision 등)을 사용할 수 있습니다.

---

## ✅ 체크리스트

- [ ] ngrok 설치 및 테스트
- [ ] 터널 URL로 `/docs` 접근 확인
- [ ] Vercel 환경변수 업데이트
- [ ] 프론트엔드 ↔ 백엔드 연결 테스트
- [ ] 워크플로우 생성 테스트

---

**마지막 업데이트**: 2025-11-30 15:30 KST
