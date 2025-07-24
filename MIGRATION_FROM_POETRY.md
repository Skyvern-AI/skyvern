# Migration from Poetry to uv

This document outlines the migration from Poetry to uv for dependency management in the Skyvern project.

## What Changed

### 1. Project Configuration
- **pyproject.toml**: Updated from Poetry format to standard PEP 621 format with uv/hatchling
- **Build system**: Switched from `poetry.core.masonry.api` to `hatchling.build`
- **Dependencies**: Converted from Poetry's caret notation (`^1.0.0`) to standard version specifiers (`>=1.0.0`)

### 2. CI/CD Workflows
- **GitHub Actions**: Replaced `snok/install-poetry` with `astral-sh/setup-uv`
- **Dependency installation**: Changed from `poetry install` to `uv pip install -e .[dev]`
- **Build commands**: Updated from `poetry build` to `uv build`

### 3. Docker
- **Multi-stage build**: Updated to use uv for requirements export instead of Poetry
- **Dependency installation**: Now uses `uv export` instead of `poetry export`

### 4. Development Scripts
- **run_skyvern.sh**: Updated to use uv for virtual environment management
- **Virtual environment**: Now uses `.venv` directory created by uv

## Benefits of uv

1. **Speed**: uv is significantly faster than Poetry for dependency resolution and installation
2. **Standards compliance**: Better adherence to Python packaging standards (PEP 621, PEP 517)
3. **Simpler toolchain**: Reduced complexity with fewer moving parts
4. **Better caching**: More efficient dependency caching in CI/CD

## Developer Migration Guide

### Prerequisites
Install uv if you haven't already:
```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# pip
pip install uv
```

### Local Development Setup

1. **Remove existing Poetry environment** (optional):
   ```bash
   rm -rf .venv
   ```

2. **Create new virtual environment with uv**:
   ```bash
   uv venv
   ```

3. **Install dependencies**:
   ```bash
   # Install all dependencies including dev dependencies
   uv pip install -e .[dev]
   
   # Or just main dependencies
   uv pip install -e .
   ```

4. **Activate virtual environment**:
   ```bash
   source .venv/bin/activate  # Linux/macOS
   # or
   .venv\\Scripts\\activate  # Windows
   ```

### Common Commands

| Poetry Command | uv Equivalent |
|----------------|---------------|
| `poetry install` | `uv pip install -e .[dev]` |
| `poetry add package` | `uv add package` |
| `poetry remove package` | `uv remove package` |
| `poetry build` | `uv build` |
| `poetry run command` | `command` (after activating venv) |
| `poetry shell` | `source .venv/bin/activate` |
| `poetry export -f requirements.txt` | `uv export --format requirements-txt` |

### Integration Packages

The integration packages (`integrations/langchain` and `integrations/llama_index`) have also been migrated:

```bash
# For langchain integration
cd integrations/langchain
uv pip install -e .[dev]

# For llama_index integration  
cd integrations/llama_index
uv pip install -e .[dev]
```

## Troubleshooting

### Virtual Environment Issues
If you encounter issues with the virtual environment:
```bash
# Remove and recreate
rm -rf .venv
uv venv
uv pip install -e .[dev]
```

### Dependency Resolution Issues
If you encounter dependency conflicts:
```bash
# Clear uv cache
uv cache clean

# Try installing with verbose output
uv pip install -e .[dev] --verbose
```

### CI/CD Issues
The CI/CD workflows have been updated to use uv. If you encounter issues:
1. Check that the `astral-sh/setup-uv@v4` action is being used
2. Ensure virtual environment activation with `source .venv/bin/activate`
3. Use `uv pip install -e .[dev]` for dependency installation

## Rollback Plan

If you need to rollback to Poetry temporarily:

1. Restore the previous `pyproject.toml` format
2. Run `poetry install` to recreate the Poetry environment
3. Update CI/CD workflows to use Poetry actions

However, the migration to uv provides significant benefits and is recommended for long-term maintenance.