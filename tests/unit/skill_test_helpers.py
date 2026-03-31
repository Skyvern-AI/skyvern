from __future__ import annotations


def first_nonempty_line_after_h1(text: str) -> str:
    """Return the first non-empty line after the top-level ``# heading`` in *text*."""
    after_h1 = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if raw_line.startswith("# "):
            after_h1 = True
            continue
        if not after_h1 or not line:
            continue
        return line
    return ""
