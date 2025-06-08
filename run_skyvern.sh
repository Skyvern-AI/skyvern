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
source "$(poetry env info --path)/bin/activate"
poetry install
./run_alembic_check.sh
poetry run python -m skyvern.forge
