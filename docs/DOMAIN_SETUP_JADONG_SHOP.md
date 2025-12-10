# jadong.shop 프로덕션 도메인 설정 가이드

## 최종 아키텍처

```
┌─────────────────────────────────────────────────────────────────┐
│                        jadong.shop                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   app.jadong.shop  ──→  Vercel (프론트엔드)                       │
│                                                                  │
│   api.jadong.shop  ──→  Cloudflare Tunnel ──→ WSL:8000 (백엔드)  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Step 1: Cloudflare에 도메인 추가

### 1.1 Cloudflare 계정 생성/로그인
1. https://dash.cloudflare.com 접속
2. 계정 없으면 무료 가입

### 1.2 도메인 추가
1. **Add a Site** 클릭
2. `jadong.shop` 입력
3. **Free** 플랜 선택
4. **Continue** 클릭

### 1.3 네임서버 변경 (가비아에서)

Cloudflare가 제공하는 네임서버로 변경해야 합니다:

```
# Cloudflare 네임서버 (예시 - 실제 값은 다를 수 있음)
ns1.cloudflare.com
ns2.cloudflare.com
```

**가비아에서 변경 방법**:
1. 가비아 → 도메인 관리 → jadong.shop
2. **네임서버 설정** 또는 **DNS 설정**
3. 기존 `ns.gabia.co.kr` 삭제
4. Cloudflare 네임서버 2개 추가
5. 저장

⚠️ **네임서버 변경은 최대 24시간 소요** (보통 1-2시간)

---

## Step 2: Cloudflare Tunnel 설정 (WSL에서)

### 2.1 cloudflared 로그인

```bash
cloudflared login
```
→ 브라우저가 열리면 `jadong.shop` 도메인 선택

### 2.2 Named Tunnel 생성

```bash
# 터널 생성
cloudflared tunnel create skyvern-prod

# 터널 ID 확인 (출력된 ID 복사)
cloudflared tunnel list
```

### 2.3 설정 파일 생성

```bash
# 터널 ID를 변수로 저장 (실제 ID로 교체)
TUNNEL_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

# 설정 파일 생성
cat > ~/.cloudflared/config.yml << EOF
tunnel: ${TUNNEL_ID}
credentials-file: /home/$(whoami)/.cloudflared/${TUNNEL_ID}.json

ingress:
  # API 백엔드 (REST + WebSocket)
  - hostname: api.jadong.shop
    service: http://localhost:8000
    originRequest:
      noTLSVerify: true

  # Catch-all (필수)
  - service: http_status:404
EOF

echo "설정 파일 생성 완료!"
cat ~/.cloudflared/config.yml
```

### 2.4 DNS 라우팅 설정

```bash
# API 서브도메인 연결
cloudflared tunnel route dns skyvern-prod api.jadong.shop
```

### 2.5 터널 테스트

```bash
# 포그라운드에서 테스트 실행
cloudflared tunnel run skyvern-prod
```

### 2.6 시스템 서비스로 등록 (영구 실행)

```bash
# 서비스 설치
sudo cloudflared service install

# 서비스 시작
sudo systemctl start cloudflared
sudo systemctl enable cloudflared

# 상태 확인
sudo systemctl status cloudflared
```

---

## Step 3: Vercel 커스텀 도메인 설정

### 3.1 Vercel Dashboard에서 도메인 추가
1. Vercel → 프로젝트 선택 → Settings → Domains
2. `app.jadong.shop` 입력
3. **Add** 클릭

### 3.2 Cloudflare DNS에 레코드 추가

Vercel이 제공하는 값으로 CNAME 추가:

| Type | Name | Content |
|------|------|---------|
| CNAME | app | cname.vercel-dns.com |

### 3.3 Vercel 환경변수 업데이트

```env
VITE_API_BASE_URL=https://api.jadong.shop/api/v1
VITE_WSS_BASE_URL=wss://api.jadong.shop/api/v1
VITE_ARTIFACT_API_BASE_URL=https://api.jadong.shop
```

---

## Step 4: 최종 확인

### 테스트 명령어

```bash
# API 테스트
curl https://api.jadong.shop/api/v1/health

# Swagger UI
echo "Open: https://api.jadong.shop/docs"

# 프론트엔드
echo "Open: https://app.jadong.shop"
```

---

## 요약: Cloudflare DNS 레코드

최종적으로 Cloudflare DNS에 다음 레코드가 설정됩니다:

| Type | Name | Content | Proxy |
|------|------|---------|-------|
| CNAME | api | [Tunnel ID].cfargotunnel.com | ✅ Proxied |
| CNAME | app | cname.vercel-dns.com | ✅ Proxied |

---

## 트러블슈팅

### 네임서버 변경 확인
```bash
dig NS jadong.shop
# cloudflare.com 네임서버가 보여야 함
```

### 터널 상태 확인
```bash
cloudflared tunnel info skyvern-prod
```

### 로그 확인
```bash
sudo journalctl -u cloudflared -f
```

---

**예상 소요 시간**:
- Cloudflare 설정: 10분
- 네임서버 전파: 1-24시간
- 터널 설정: 10분
- Vercel 도메인: 5분

**총 비용**: $0 (도메인 비용 제외)
