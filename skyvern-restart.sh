#!/bin/bash

# Skyvern 재시작 스크립트

echo "🔄 Skyvern 재시작 중..."

# 기존 프로세스 종료
./skyvern-stop.sh

echo ""
echo "5초 대기 중..."
sleep 5

# PostgreSQL 시작 확인
if ! docker ps | grep -q skyvern-postgres; then
    echo "📦 PostgreSQL 시작 중..."
    docker start skyvern-postgres 2>/dev/null || \
    docker run -d --name skyvern-postgres \
        -e POSTGRES_USER=skyvern \
        -e POSTGRES_PASSWORD=skyvern \
        -e POSTGRES_DB=skyvern \
        -e PGDATA=/var/lib/postgresql/data/pgdata \
        -v "$(pwd)/postgres-data:/var/lib/postgresql/data" \
        -p 5432:5432 \
        postgres:14-alpine
    
    echo "PostgreSQL 준비 대기 중..."
    sleep 10
fi

# Skyvern 시작
echo "🚀 Skyvern 시작 중..."
cd "$(dirname "$0")" || exit 1
nohup uv run skyvern run all > skyvern.log 2>&1 &
echo "PID: $!"

echo ""
echo "30초 대기 중..."
sleep 30

echo ""
echo "✅ Skyvern이 재시작되었습니다!"
echo ""
echo "상태 확인: ./skyvern-status.sh"
echo "접속: http://localhost:8080"

