#!/bin/bash

pid=$(lsof -t -i :8080)
if [ -n "$pid" ]; then
  kill "$pid"
fi

cd skyvern-frontend || exit 1

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[ERROR] Please add your api keys to the skyvern-frontend/.env file."
fi

npm install --silent
npm run start
