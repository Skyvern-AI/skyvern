#!/usr/bin/env bash
# This script loads all variables from .env into the current shell environment (Windows compatible)
# Usage: source ./load_env.sh

set -a
if [ -f .env ]; then
  # Remove comments and blank lines, then export each variable
  # Uses input redirection so the while loop runs in the current shell
  while IFS= read -r line; do
    # Skip comments and blank lines
    case "$line" in
      \#*|"") continue ;;
    esac
    # Only process lines with an equals sign
    if echo "$line" | grep -q '='; then
      varname=$(echo "$line" | cut -d '=' -f 1)
      varvalue=$(echo "$line" | cut -d '=' -f 2-)
      # Remove any surrounding quotes
      varvalue=$(echo "$varvalue" | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
      export "$varname=$varvalue"
    fi
  done < .env
fi
set +a
