#!/bin/bash

# Skyvern Docker 시작 스크립트
# WSL2에서 Windows Docker를 사용하여 Skyvern을 실행합니다

echo "🐉 Skyvern Docker 시작 중..."
echo ""

# Docker 경로 설정
DOCKER="/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe"
DOCKER_COMPOSE="/mnt/c/Program Files/Docker/Docker/resources/bin/docker-compose.exe"

# Docker가 실행 중인지 확인
echo "📊 Docker 상태 확인 중..."
if ! "$DOCKER" ps >/dev/null 2>&1; then
    echo "⚠️  Docker Desktop이 실행되지 않았습니다."
    echo "    Docker Desktop을 시작하는 중..."
    powershell.exe -Command "Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'" >/dev/null 2>&1
    echo "    30초 대기 중..."
    sleep 30
    
    # 다시 확인
    if ! "$DOCKER" ps >/dev/null 2>&1; then
        echo "❌ Docker Desktop을 시작할 수 없습니다."
        echo "   수동으로 Docker Desktop을 시작한 후 다시 시도하세요."
        exit 1
    fi
fi

echo "✅ Docker가 정상적으로 실행 중입니다."
echo ""

# 프로젝트 디렉토리로 이동
cd "$(dirname "$0")" || exit 1

# 필수 디렉토리 확인 및 생성
echo "📁 필수 디렉토리 확인 중..."
mkdir -p artifacts videos har log postgres-data .streamlit

# .env 파일 확인
echo "🔍 환경 설정 확인 중..."
if [ ! -f .env ]; then
    echo "⚠️  .env 파일이 없습니다. .env.example을 복사합니다..."
    cp .env.example .env
fi

# LLM 설정 확인
if ! grep -q "ENABLE_OPENAI=true\|ENABLE_ANTHROPIC=true\|ENABLE_GEMINI=true\|ENABLE_AZURE=true" .env 2>/dev/null; then
    echo ""
    echo "⚠️  LLM API 키가 설정되지 않았습니다!"
    echo "   .env 파일을 수정하여 LLM 제공자를 활성화하고 API 키를 입력하세요."
    echo ""
    echo "   예시:"
    echo "   ENABLE_OPENAI=true"
    echo "   OPENAI_API_KEY=\"your-api-key-here\""
    echo "   LLM_KEY=\"OPENAI_GPT4O\""
    echo ""
    echo "   자세한 내용은 SKYVERN_실행가이드.md를 참조하세요."
    echo ""
    echo "   ℹ️  API 키 없이도 서비스는 시작됩니다."
    echo "      단, 실제 작업을 수행하려면 API 키가 필요합니다."
    echo ""
fi

# Windows 경로로 변환
WIN_PATH=$(wslpath -w "$(pwd)")
echo "📂 프로젝트 경로: $WIN_PATH"
echo ""

# Docker Compose 실행
echo "🚀 Docker Compose 시작 중..."
echo "   이 작업은 처음 실행 시 몇 분이 걸릴 수 있습니다..."
echo ""

# PowerShell을 통해 Windows 경로에서 Docker Compose 실행
if powershell.exe -Command "cd '$WIN_PATH'; & 'C:\Program Files\Docker\Docker\resources\bin\docker-compose.exe' up -d"; then
    echo ""
    echo "✅ Skyvern이 성공적으로 시작되었습니다!"
    echo ""
    echo "📊 서비스 상태 확인 중..."
    sleep 5
    "$DOCKER_COMPOSE" ps
    echo ""
    echo "🌐 접속 정보:"
    echo "   - Skyvern UI: http://localhost:8080"
    echo "   - API 서버: http://localhost:8000"
    echo "   - API 문서: http://localhost:8000/docs"
    echo ""
    echo "📋 유용한 명령어:"
    echo "   로그 확인: ./docker-logs.sh"
    echo "   서비스 중지: ./docker-stop.sh"
    echo ""
    echo "자세한 사용법은 SKYVERN_실행가이드.md를 참조하세요."
else
    echo ""
    echo "❌ Docker Compose 시작 실패"
    echo "   로그를 확인하세요: '$DOCKER_COMPOSE' logs"
    exit 1
fi

