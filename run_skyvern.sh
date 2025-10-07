#!/bin/bash

pid=$(lsof -t -i :8000)
if [ -n "$pid" ]; then
  kill "$pid"
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Please add your api keys to the .env file."
fi
# shellcheck source=/dev/null
uv sync
./run_alembic_check.sh
uv run python -m skyvern.forge
