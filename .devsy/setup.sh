#!/bin/bash

# Devsy Setup Script Example
# 
# Copy this file to .devsy/setup.sh in your repository and uncomment the sections
# relevant to your project. This script runs after Python environment setup
# but before Claude Code execution to ensure dependencies are available.
#
# Usage in your workflow:
#   - uses: DevsyAI/devsy-action@main
#     with:
#       setup_script: '.devsy/setup.sh'

echo "🔧 Running Devsy setup..."

# ============================================================================
# PYTHON PROJECTS
# ============================================================================

# Install Python dependencies (uncomment ONE of these based on your project)
# pip install -r requirements.txt
# pip install -r requirements-dev.txt
# pip install -e .
# uv sync
# uv sync --group dev

# ============================================================================
# NODE.JS / JAVASCRIPT / TYPESCRIPT PROJECTS
# ============================================================================

# Install Node dependencies (uncomment as needed)
# npm install
# npm ci  # For faster, reproducible builds
# yarn install
# pnpm install

# Build if needed (uncomment as needed)
# npm run build
# npm run compile

# ============================================================================
# RUBY PROJECTS
# ============================================================================

# Install Ruby dependencies (uncomment as needed)
# bundle install
# gem install bundler && bundle install

# ============================================================================
# GO PROJECTS
# ============================================================================

# Download Go dependencies (uncomment as needed)
# go mod download
# go mod tidy

# ============================================================================
# RUST PROJECTS
# ============================================================================

# Build Rust dependencies (uncomment as needed)
# cargo build
# cargo fetch

# ============================================================================
# JAVA PROJECTS
# ============================================================================

# Build Java projects (uncomment based on your build tool)
# mvn compile
# mvn install -DskipTests
# ./gradlew build
# ./gradlew assemble

# ============================================================================
# ENVIRONMENT SETUP
# ============================================================================

# Create .env file from example (uncomment if needed)
# if [ -f ".env.example" ] && [ ! -f ".env" ]; then
#     cp .env.example .env
# fi

# Make scripts executable (uncomment if you have scripts)
# chmod +x scripts/*.sh 2>/dev/null || true
# chmod +x bin/* 2>/dev/null || true

echo "✅ Devsy setup completed!"