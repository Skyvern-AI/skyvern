#!/bin/bash

# Skyvern Docker 중지 스크립트

echo "🛑 Skyvern Docker 중지 중..."

# Docker Compose 경로 (스크립트에서 직접 사용하므로 변수 불필요)

# 프로젝트 디렉토리로 이동
cd "$(dirname "$0")" || exit 1

# Windows 경로로 변환
WIN_PATH=$(wslpath -w "$(pwd)")

# Docker Compose 중지
if powershell.exe -Command "cd '$WIN_PATH'; & 'C:\Program Files\Docker\Docker\resources\bin\docker-compose.exe' down"; then
    echo "✅ Skyvern이 성공적으로 중지되었습니다."
else
    echo "❌ 중지 실패. 수동으로 확인하세요."
    exit 1
fi

