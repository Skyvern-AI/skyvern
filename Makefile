# Makefile for Skyvern development and deployment tasks

.PHONY: help requirements docker-build docker-build-ui docker-test clean install dev test lint format

# Default target
help:
	@echo "Available commands:"
	@echo "  requirements      Generate locked requirements.txt using uv"
	@echo "  docker-build      Build the main Docker image"
	@echo "  docker-build-ui   Build the UI Docker image"
	@echo "  docker-test       Test Docker build with uv requirements"
	@echo "  install           Install dependencies using Poetry"
	@echo "  dev               Start development server"
	@echo "  test              Run tests"
	@echo "  lint              Run linting and formatting checks"
	@echo "  format            Format code using ruff"
	@echo "  clean             Clean generated files"

# Generate locked requirements.txt using uv
requirements:
	@echo "Generating requirements.txt using uv..."
	@chmod +x scripts/generate_requirements.sh
	@./scripts/generate_requirements.sh

# Build Docker images
docker-build: requirements
	@echo "Building main Docker image..."
	docker build -t skyvern:latest .

docker-build-ui:
	@echo "Building UI Docker image..."
	docker build -f Dockerfile.ui -t skyvern-ui:latest .

# Test Docker build
docker-test:
	@echo "Testing Docker build with uv..."
	@chmod +x scripts/test_docker_build.sh
	@./scripts/test_docker_build.sh

# Development commands
install:
	@echo "Installing dependencies with Poetry..."
	poetry install

dev:
	@echo "Starting development server..."
	poetry run python -m skyvern.cli.main

test:
	@echo "Running tests..."
	poetry run pytest

lint:
	@echo "Running pre-commit hooks..."
	poetry run pre-commit run --all-files

format:
	@echo "Formatting code with ruff..."
	poetry run ruff check --fix .
	poetry run ruff format .

# Clean generated files
clean:
	@echo "Cleaning generated files..."
	rm -f requirements.txt
	rm -rf .venv
	rm -rf __pycache__
	rm -rf .pytest_cache
	rm -rf .ruff_cache
	find . -name "*.pyc" -delete
	find . -name "*.pyo" -delete
	find . -name "__pycache__" -type d -exec rm -rf {} +