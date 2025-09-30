#!/bin/bash
# Test script to validate Docker build with uv-generated requirements.txt

set -euo pipefail

echo "🐳 Testing Docker build with uv-generated requirements.txt"
echo "=================================================="

# Generate requirements.txt first
echo "📦 Generating requirements.txt using uv..."
if ! command -v uv &> /dev/null; then
    echo "Installing uv..."
    pip install uv
fi

uv pip compile pyproject.toml --output-file requirements.txt --generate-hashes --quiet

if [[ ! -f "requirements.txt" ]]; then
    echo "❌ Failed to generate requirements.txt"
    exit 1
fi

echo "✅ requirements.txt generated successfully"
echo "📊 File size: $(wc -c < requirements.txt) bytes"
echo "📦 Dependencies: $(grep -c "^[a-zA-Z]" requirements.txt || echo "0") packages"

# Test Docker build (first stage only for speed)
echo ""
echo "🔨 Testing Docker build (requirements stage only)..."
docker build --target requirements-stage -t skyvern-requirements-test . || {
    echo "❌ Docker build failed"
    exit 1
}

echo "✅ Docker requirements stage built successfully"

# Clean up test image
echo "🧹 Cleaning up test image..."
docker rmi skyvern-requirements-test || true

echo ""
echo "🎉 All tests passed! Docker build with uv is working correctly."
echo ""
echo "To build the full image:"
echo "  docker build -t skyvern:latest ."
echo ""
echo "To build using Makefile:"
echo "  make docker-build"