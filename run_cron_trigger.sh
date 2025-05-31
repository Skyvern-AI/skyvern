#!/bin/bash

source "$(poetry env info --path)/bin/activate"
poetry install
poetry run python -m skyvern.cron_trigger
