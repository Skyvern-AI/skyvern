#!/bin/bash
# skyvern-ngrok.sh - ngrok 터널 시작 스크립트
# WSL2 환경에서 Cloudflare Tunnel 대신 사용

set -e

echo "🔍 Skyvern 서버 상태 확인..."

# Skyvern 서버 실행 확인
if ! curl -s http://localhost:8000/docs > /dev/null 2>&1; then
    echo "❌ Skyvern 서버가 실행 중이 아닙니다!"
    echo ""
    echo "먼저 다음 명령어로 서버를 시작하세요:"
    echo "  ./skyvern-restart.sh"
    echo "  또는"
    echo "  skyvern run server"
    exit 1
fi

echo "✅ Skyvern 서버 실행 중 (localhost:8000)"
echo ""

# ngrok 설치 확인
if ! command -v ngrok &> /dev/null; then
    echo "❌ ngrok이 설치되어 있지 않습니다!"
    echo ""
    echo "설치 방법:"
    echo "  curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | \\"
    echo "    sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null && \\"
    echo "    echo 'deb https://ngrok-agent.s3.amazonaws.com buster main' | \\"
    echo "    sudo tee /etc/apt/sources.list.d/ngrok.list && \\"
    echo "    sudo apt update && sudo apt install ngrok"
    echo ""
    echo "설치 후 인증:"
    echo "  ngrok config add-authtoken YOUR_NGROK_AUTH_TOKEN"
    echo "  (https://dashboard.ngrok.com/get-started/your-authtoken 에서 토큰 확인)"
    exit 1
fi

echo "🚀 ngrok 터널 시작..."
echo ""
echo "터널이 시작되면 Forwarding URL을 확인하세요!"
echo "예: https://xxxx-xxx-xxx.ngrok-free.app"
echo ""
echo "Vercel 환경변수 설정:"
echo "  VITE_API_BASE_URL=<ngrok-url>/api/v1"
echo "  VITE_WSS_BASE_URL=<ngrok-url을 wss://로 변경>/api/v1"
echo ""
echo "종료하려면 Ctrl+C"
echo "=========================================="
echo ""

ngrok http 8000
