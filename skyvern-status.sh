#!/bin/bash

# Skyvern 상태 확인 스크립트

echo "📊 Skyvern 서비스 상태"
echo "========================"
echo ""

# 프로세스 확인
echo "🔍 실행 중인 프로세스:"
pgrep -a "skyvern|postgres" | awk '{printf "  %-8s %s\n", $1, $2}' || echo "  (프로세스 없음)"
echo ""

# 포트 확인
echo "🌐 포트 상태:"
for port in 8000 8080 5432; do
    if lsof -i :$port -sTCP:LISTEN -t >/dev/null 2>&1; then
        echo "  ✅ 포트 $port: 열림"
    else
        echo "  ❌ 포트 $port: 닫힘"
    fi
done
echo ""

# 서비스 테스트
echo "🧪 서비스 연결 테스트:"

# API 서버
if curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/docs | grep -q "200"; then
    echo "  ✅ API 서버 (8000): 정상"
else
    echo "  ❌ API 서버 (8000): 응답 없음"
fi

# UI 서버
if curl -s -o /dev/null -w "%{http_code}" http://localhost:8080 | grep -q "302\|200"; then
    echo "  ✅ UI 서버 (8080): 정상"
else
    echo "  ❌ UI 서버 (8080): 응답 없음"
fi

# PostgreSQL
if docker exec skyvern-postgres pg_isready -U skyvern >/dev/null 2>&1; then
    echo "  ✅ PostgreSQL (5432): 정상"
else
    echo "  ❌ PostgreSQL (5432): 응답 없음"
fi
echo ""

# 로그 마지막 줄
echo "📋 최근 로그 (마지막 5줄):"
if [ -f skyvern.log ]; then
    tail -5 skyvern.log | sed 's/^/  /'
else
    echo "  로그 파일 없음"
fi

