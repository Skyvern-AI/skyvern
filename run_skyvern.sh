#!/bin/bash

# Activate Python virtual environment
source "$(poetry env info --path)/bin/activate"

# Kill any process running on port 8000
kill $(lsof -t -i :8000)

# Check if .env file exists, if not, copy .env.example
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Please add your API keys to the .env file."
fi

# Activate Python virtual environment (again)
source "$(poetry env info --path)/bin/activate"

# Install project dependencies using Poetry
poetry install

# Execute database migration checks
./run_alembic_check.sh

# Run Streamlit app
streamlit run streamlit_app/visualizer/streamlit.py -- $@

# Run Python module skyvern.forge-[optimize] using Poetry
poetry run python -m skyvern.forge-[optimize]
