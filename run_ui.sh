#!/bin/bash

kill $(lsof -t -i :8080)

cd skyvern-frontend

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[ERROR] Please add your api keys to the skyvern-frontend/.env file."
fi

npm install --silent
npm run start
