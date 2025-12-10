# 🚨 인프라 배포 이슈 보고서

**작성일**: 2025-11-30 14:57 KST  
**작성자**: Cursor (Local Builder)  
**수신**: Claude Code (Cloud Architect)  
**상태**: 🔴 **긴급 - 해결 필요**

---

## 📋 작업 요약

### 완료된 작업 ✅

1. **Claude 브랜치 병합** - 성공
   - `origin/claude/analyze-skyvern-architecture-01NX7mN8NWfWdASX7CUbsFun` 브랜치 병합 완료
   - 파일 추가됨:
     - `DEPLOYMENT_ARCHITECTURE.md`
     - `SKYVERN_ARCHITECTURE_ANALYSIS.md`
     - `skyvern-frontend/vercel.json`
     - `skyvern-frontend/.env.production.example`

2. **Vercel 프론트엔드 배포** - 성공
   - URL: `https://skyvern-frontend-xxxxxx.vercel.app`
   - 빌드 및 배포 정상 완료
   - 환경변수 설정 완료

3. **로컬 백엔드 서버** - 성공
   - PostgreSQL: Docker 컨테이너 실행 중 (포트 15432)
   - Skyvern API: localhost:8000에서 정상 실행
   - 데이터베이스 마이그레이션 완료
   - 조직 및 API 키 생성 완료

---

## 🔴 현재 문제점

### 핵심 이슈: Cloudflare Tunnel 연결 실패

**증상:**
- Cloudflare Quick Tunnel URL 생성됨 (예: `https://piano-festivals-came-minimum.trycloudflare.com`)
- 터널 프로세스 정상 실행 중 (Registered tunnel connection 확인)
- **하지만 터널 URL로 요청 시 404 Not Found 반환**

**진단 결과:**

```bash
# 로컬 테스트 - 성공 ✅
$ curl http://localhost:8000/docs | grep title
<title>FastAPI - Swagger UI</title>

$ curl http://127.0.0.1:8000/docs | grep title
<title>FastAPI - Swagger UI</title>

# 터널 테스트 - 실패 ❌
$ curl -sI https://piano-festivals-came-minimum.trycloudflare.com/docs
HTTP/2 404
server: cloudflare
```

**서버 바인딩 확인:**
```bash
$ ss -tlnp | grep 8000
LISTEN 0 2048 0.0.0.0:8000 0.0.0.0:* users:(("skyvern",pid=39586,fd=15))
```
→ 서버가 0.0.0.0에 바인딩되어 있어 외부 접근 가능해야 함

**터널 로그:**
```
2025-11-30T05:56:42Z INF | https://piano-festivals-came-minimum.trycloudflare.com |
2025-11-30T05:56:43Z INF Registered tunnel connection connIndex=0 ... location=icn06 protocol=quic
```
→ 터널 자체는 정상 등록됨

---

## 🔍 추정 원인

### 가설 1: WSL2 네트워킹 문제
- WSL2의 가상 네트워크와 cloudflared 간의 라우팅 문제
- localhost vs 127.0.0.1 vs WSL2 IP 주소 차이

### 가설 2: Cloudflare Quick Tunnel 제한
- Quick Tunnel (무료)의 안정성/연결 문제
- QUIC 프로토콜과 WSL2 호환성 문제

### 가설 3: 방화벽/포트 포워딩
- Windows 방화벽이 트래픽 차단
- WSL2 → Windows 간 포트 포워딩 설정 필요

---

## 📊 현재 시스템 상태

### 실행 중인 서비스

| 서비스 | 포트 | 상태 | 비고 |
|--------|------|------|------|
| PostgreSQL | 15432 | ✅ 실행 중 | Docker 컨테이너 |
| Skyvern API | 8000 | ✅ 실행 중 | localhost 접근 가능 |
| Cloudflare Tunnel | - | ⚠️ 문제 있음 | 연결은 되나 요청 전달 안됨 |
| Vercel Frontend | - | ✅ 배포됨 | 백엔드 연결 불가 |

### 환경 정보

```
OS: WSL2 (Ubuntu) on Windows
Docker: Docker Desktop 28.1.1
Python: 3.12.3
Node.js: 22.20.0
cloudflared: 2025.9.1
```

### 생성된 API 키

```
Organization ID: o_467286173652022586
API Key: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
(만료: 100년 후)
```

---

## 🛠 시도한 해결책

1. **터널 재시작** - 실패
   - 여러 번 재시작해도 동일 증상

2. **localhost → 127.0.0.1 변경** - 실패
   - cloudflared tunnel --url http://127.0.0.1:8000
   - 동일하게 404 반환

3. **터널 URL 갱신** - 실패
   - 새 URL 생성해도 동일 문제

---

## 📋 필요한 조치

### 우선순위 1: 터널링 문제 해결

**옵션 A: ngrok 사용**
```bash
# ngrok 설치 및 사용
ngrok http 8000
```

**옵션 B: Cloudflare Named Tunnel**
```bash
# 계정 로그인 및 Named Tunnel 생성
cloudflared login
cloudflared tunnel create skyvern-backend
```

**옵션 C: 직접 포트 포워딩**
- 라우터/공유기 설정에서 포트 포워딩
- 또는 VPS 서버에 백엔드 배포

### 우선순위 2: 대안 아키텍처 검토

프론트엔드와 백엔드를 동일 환경에서 실행:
1. 둘 다 로컬에서 실행 (localhost:8080, localhost:8000)
2. 둘 다 클라우드에 배포 (Railway, Render, Fly.io 등)

---

## 📁 관련 파일

- `/home/tlswk/projects/skyvern/.env` - 백엔드 환경 설정
- `/home/tlswk/projects/skyvern/skyvern-frontend/.env.production.local` - 프론트엔드 환경 설정
- `/home/tlswk/projects/skyvern/tunnel.log` - 터널 로그
- `/home/tlswk/projects/skyvern/skyvern-server.log` - 서버 로그

---

## 🎯 요청 사항

**Claude Code (Cloud)에게:**

1. WSL2 환경에서 Cloudflare Tunnel이 작동하지 않는 원인 분석
2. 대안 터널링 솔루션 제안 (ngrok, localtunnel 등)
3. 또는 완전한 클라우드 배포 방안 제안 (백엔드도 클라우드에 배포)

---

## 📞 연락처

- **작업 환경**: WSL2 Ubuntu + Cursor IDE
- **GitHub Repo**: https://github.com/shinjadong/skyvern
- **현재 브랜치**: main

---

**마지막 업데이트**: 2025-11-30 14:57 KST

