#!/bin/bash

pid=$(lsof -t -i :8000)
if [ -n "$pid" ]; then
  kill "$pid"
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Please add your api keys to the .env file."
fi
# Create virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
  uv venv
fi

# shellcheck source=/dev/null
source .venv/bin/activate
uv pip install -e .[dev]
./run_alembic_check.sh
python -m skyvern.forge
