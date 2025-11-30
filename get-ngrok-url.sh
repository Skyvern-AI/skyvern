#!/bin/bash
# get-ngrok-url.sh - ngrok URL 확인 및 Vercel 환경변수 형식 출력

# ngrok API에서 터널 정보 가져오기
NGROK_API="http://localhost:4040/api/tunnels"

# ngrok 실행 확인
if ! curl -s "$NGROK_API" > /dev/null 2>&1; then
    echo "❌ ngrok이 실행 중이 아닙니다!"
    echo ""
    echo "먼저 다른 터미널에서 실행:"
    echo "  ./skyvern-ngrok.sh"
    exit 1
fi

# URL 추출
NGROK_URL=$(curl -s "$NGROK_API" | grep -oP '"public_url":"https://[^"]+' | head -1 | cut -d'"' -f4)

if [ -z "$NGROK_URL" ]; then
    echo "❌ ngrok URL을 가져올 수 없습니다!"
    exit 1
fi

# WSS URL 생성
WSS_URL=$(echo "$NGROK_URL" | sed 's/https/wss/')

echo "=========================================="
echo "🌐 ngrok 터널 URL"
echo "=========================================="
echo ""
echo "📍 Public URL: $NGROK_URL"
echo ""
echo "=========================================="
echo "📝 Vercel 환경변수 (복사해서 사용)"
echo "=========================================="
echo ""
echo "VITE_API_BASE_URL=${NGROK_URL}/api/v1"
echo "VITE_WSS_BASE_URL=${WSS_URL}/api/v1"
echo "VITE_ARTIFACT_API_BASE_URL=${NGROK_URL}"
echo ""
echo "=========================================="
echo "🧪 테스트 명령어"
echo "=========================================="
echo ""
echo "# API 테스트"
echo "curl ${NGROK_URL}/api/v1/health"
echo ""
echo "# Swagger UI"
echo "echo 'Open in browser: ${NGROK_URL}/docs'"
echo ""
