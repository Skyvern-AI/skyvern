# Dependency Management with uv

This document explains how Skyvern uses `uv` to generate locked `requirements.txt` files for faster and more reliable Docker builds.

## Overview

Skyvern uses Poetry for development dependency management but generates locked `requirements.txt` files using `uv` for Docker deployments. This hybrid approach provides:

- **Development**: Full Poetry ecosystem with dev dependencies, virtual environments, and dependency resolution
- **Production**: Fast, reliable Docker builds with locked requirements and hash verification

## Why uv?

- **Speed**: uv is significantly faster than pip for dependency resolution and installation
- **Reliability**: Hash verification ensures package integrity
- **Reproducibility**: Locked versions with exact hashes guarantee consistent builds
- **Compatibility**: Works seamlessly with existing Poetry workflows

## Generating requirements.txt

### Using the Makefile (Recommended)

```bash
make requirements
```

### Using the Script Directly

```bash
chmod +x scripts/generate_requirements.sh
./scripts/generate_requirements.sh
```

### Using Python Script

```bash
python generate_requirements.py
```

### Manual uv Command

```bash
pip install uv
uv pip compile pyproject.toml --output-file requirements.txt --generate-hashes
```

## Docker Build Process

The updated Dockerfile now:

1. **Stage 1**: Uses `uv pip compile` to generate `requirements.txt` from `pyproject.toml`
2. **Stage 2**: Uses `uv pip install` for faster package installation with hash verification

### Building Docker Images

```bash
# Generate requirements.txt and build
make docker-build

# Or build directly (requirements.txt must exist)
docker build -t skyvern:latest .
```

## CI/CD Integration

To integrate with CI/CD pipelines, add a step to generate `requirements.txt` before Docker builds:

```yaml
- name: Generate requirements.txt with uv
  run: |
    pip install uv
    uv pip compile pyproject.toml --output-file requirements.txt --generate-hashes
```

## File Structure

```
skyvern/
├── pyproject.toml          # Poetry configuration (source of truth)
├── poetry.lock            # Poetry lock file (for development)
├── requirements.txt       # Generated locked requirements (for Docker)
├── generate_requirements.py  # Python script for requirements generation
├── scripts/
│   └── generate_requirements.sh  # Bash script for requirements generation
└── Makefile              # Convenient commands
```

## Best Practices

1. **Always regenerate** `requirements.txt` after updating `pyproject.toml`
2. **Commit** `requirements.txt` to version control for reproducible builds
3. **Use Poetry** for development and dependency management
4. **Use uv-generated requirements.txt** for Docker builds and production deployments
5. **Verify hashes** are included in `requirements.txt` for security

## Troubleshooting

### uv not found
```bash
pip install uv
```

### Requirements generation fails
1. Ensure `pyproject.toml` is valid
2. Check for dependency conflicts
3. Try updating Poetry lock first: `poetry lock --no-update`

### Docker build fails
1. Ensure `requirements.txt` exists and is up-to-date
2. Verify all dependencies are available
3. Check for platform-specific dependencies

## Migration from Poetry Export

The new approach replaces:
```dockerfile
RUN poetry export -f requirements.txt --output requirements.txt --without-hashes
```

With:
```dockerfile
RUN uv pip compile pyproject.toml --output-file requirements.txt --generate-hashes
```

This provides better performance and security through hash verification.