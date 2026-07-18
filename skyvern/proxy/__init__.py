"""CDP proxy service: a hexagonal core with pluggable ports and adapters.

Layering is enforced by import-linter (see pyproject.toml): `core` and `ports`
are pure (no framework or I/O imports); `adapters` depend on them, never the reverse.
"""
