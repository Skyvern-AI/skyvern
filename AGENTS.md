# Skyzona Agent Guide

## Build/Lint/Test Commands
- Single test: `pytest tests/unit_tests/test_file.py::test_function -v`
- Full suite: `pytest`
- Lint + format: `ruff check --fix && ruff format`
- Type checks: `mypy skyvern/`
- Pre-flight: `pre-commit run --all-files` before shipping
- Runtime mgmt: only `pm2 start|restart|stop|logs`; never `npm start` or `node server.js`

## Code Style Guidelines
- Imports: absolute paths sorted via isort (black profile)
- Formatting: Ruff formatter, 120 char max, keep docstrings concise
- Types: Python 3.11+, annotate everything including literals and Protocols
- Naming: snake_case vars/funcs, PascalCase classes, CONSTANTS screaming snake
- Error handling: raise specific exceptions, meaningful messages, omit sensitive data
- Async: async/await with context managers, ensure cleanup on cancellation
- Logging: use project loggers, structured fields preferred over f-strings
- Testing: add deterministic tests for new behavior and edge cases
- Dependencies: avoid ad-hoc versions; update pyproject/lockfiles consistently
