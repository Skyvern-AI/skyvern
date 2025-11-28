#!/bin/bash

# Skyvern 중지 스크립트

echo "🛑 Skyvern 서비스 중지 중..."

# Skyvern 프로세스 종료
pkill -f "skyvern run" && echo "✅ Skyvern 프로세스 종료됨" || echo "⚠️  Skyvern 프로세스 없음"

# PostgreSQL 컨테이너 중지
docker stop skyvern-postgres 2>/dev/null && echo "✅ PostgreSQL 중지됨" || echo "⚠️  PostgreSQL 컨테이너 없음"

echo ""
echo "서비스가 중지되었습니다."

