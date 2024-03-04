#!/bin/bash

kill $(lsof -t -i :8000)

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Please add your api keys to the .env file."
fi
source "$(poetry env info --path)/bin/activate"
python scripts/tracking.py skyvern-oss-run-server
poetry run python -m skyvern.forge
