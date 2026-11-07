#!/usr/bin/env bash
# This script loads all variables from .env into the current shell environment
# On Windows, use WSL, Git Bash, or a similar Bash-compatible shell
# Usage: source ./load_env.sh

if [ -f .env ]; then
  # Remove comments and blank lines, then export each variable
  # Uses input redirection so the while loop runs in the current shell
  # `|| [ -n "$line" ]` keeps the last line when .env has no trailing newline
  while IFS= read -r line || [ -n "$line" ]; do
    # Skip comments and blank lines
    case "$line" in
      \#*|"") continue ;;
    esac
    # Only process lines with an equals sign
    if echo "$line" | grep -q '='; then
      varname=$(echo "$line" | cut -d '=' -f 1)
      # Accept dotenv `export KEY=value` lines and trim surrounding whitespace
      varname=$(echo "$varname" | sed -e 's/^[[:space:]]*//' -e 's/^export[[:space:]]\{1,\}//' -e 's/[[:space:]]*$//')
      varvalue=$(echo "$line" | cut -d '=' -f 2-)
      # Remove any surrounding quotes
      varvalue=$(echo "$varvalue" | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
      export "$varname=$varvalue"
    fi
  done < .env
fi
unset line varname varvalue
