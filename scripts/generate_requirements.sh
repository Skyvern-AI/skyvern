#!/bin/bash
# Script to generate locked requirements.txt using uv from pyproject.toml
# This script can be used in CI/CD pipelines and local development

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "Generating requirements.txt from pyproject.toml..."
echo "Project directory: $PROJECT_DIR"

# Change to project directory
cd "$PROJECT_DIR"

# Check if pyproject.toml exists
if [[ ! -f "pyproject.toml" ]]; then
    echo "❌ Error: pyproject.toml not found in $PROJECT_DIR"
    exit 1
fi

# Install uv if not already available
if ! command -v uv &> /dev/null; then
    echo "Installing uv..."
    pip install uv
fi

# Generate requirements.txt using uv
echo "Generating requirements.txt with dependency hashes..."
uv pip compile pyproject.toml \
    --output-file requirements.txt \
    --generate-hashes \
    --quiet

# Verify the generated file
if [[ -f "requirements.txt" ]]; then
    file_size=$(wc -c < requirements.txt)
    echo "✅ Successfully generated requirements.txt"
    echo "📊 File size: $file_size bytes"
    echo "📦 Dependencies locked with hashes for reproducible builds"
else
    echo "❌ Failed to generate requirements.txt"
    exit 1
fi