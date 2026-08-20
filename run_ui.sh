#!/bin/bash

pid=$(lsof -t -i :8080)
if [ -n "$pid" ]; then
  kill "$pid"
fi

cd skyvern-frontend || exit 1

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[ERROR] Set SKYVERN_API_KEY in skyvern-frontend/.env."
fi

npm install --silent
npm run start
