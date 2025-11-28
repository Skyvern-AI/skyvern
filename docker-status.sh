#!/bin/bash

# Skyvern Docker 상태 확인 스크립트

# Docker Compose 경로 (스크립트에서 직접 사용하므로 변수 불필요)

# 프로젝트 디렉토리로 이동
cd "$(dirname "$0")" || exit 1

# Windows 경로로 변환
WIN_PATH=$(wslpath -w "$(pwd)")

echo "📊 Skyvern Docker 상태"
echo ""

# 상태 확인
powershell.exe -Command "cd '$WIN_PATH'; & 'C:\Program Files\Docker\Docker\resources/bin\docker-compose.exe' ps"

