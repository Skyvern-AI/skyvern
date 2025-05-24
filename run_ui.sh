#!/bin/bash

if command -v lsof > /dev/null; then
  pid=$(lsof -t -i :8080)
  kill $pid 2>/dev/null || true
else
  echo "Warning: lsof command not found, skipping port check"
fi

cd skyvern-frontend

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[ERROR] Please add your api keys to the skyvern-frontend/.env file."
fi

npm ci --silent
npm run start
