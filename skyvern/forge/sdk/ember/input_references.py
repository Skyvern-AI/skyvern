from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping
from typing import Any

from jinja2 import TemplateError, meta

from skyvern.forge.sdk.workflow.models.block import jinja_sandbox_env

_JINJA_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
# Fallback only: this sees the identifier that opens an expression, so `{{ a ~ b }}` hides `b`.
# jinja_roots parses instead, and falls back to this when the text is not valid Jinja.
JINJA_ROOT_RE = re.compile(rf"\{{\{{\s*({_JINJA_IDENTIFIER})")

CREDENTIAL_REAL_VALUE_SUFFIXES = ("_real_username", "_real_password")

_STRUCTURAL_BLOCK_FIELDS = frozenset({"label", "block_type", "next_block_label"})

# Jinja analysis hides roots named like a global, so an input named `range` would read as unreferenced.
_ANALYSIS_ENV = jinja_sandbox_env.overlay()
_ANALYSIS_ENV.globals = {}


def jinja_string_leaves(serialized: str) -> list[str]:
    """The block config's own string values. The serialized form is JSON, so a quoted Jinja literal
    arrives escaped as ``\\"`` — which no Jinja parser accepts — and parsing the whole blob would cost
    every reference in a block that uses one."""
    try:
        payload = json.loads(serialized)
    except (json.JSONDecodeError, TypeError, ValueError):
        return [serialized]
    leaves: list[str] = []
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            leaves.append(item)
        elif isinstance(item, dict):
            # Keys too: error_code_mapping is templatable on both sides, so a key can carry a reference.
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return leaves


def jinja_roots(serialized: str) -> set[str]:
    """Every identifier the config's Jinja expressions read, parsed the way runs render them. Falls back to the
    opening-identifier regex for a leaf that does not parse, which keeps a malformed block classifiable
    rather than silently referenceless."""
    roots: set[str] = set()
    for leaf in jinja_string_leaves(serialized):
        try:
            roots |= meta.find_undeclared_variables(_ANALYSIS_ENV.parse(leaf))
        except (TemplateError, RecursionError):
            roots |= set(JINJA_ROOT_RE.findall(leaf))
    return roots


def root_parameter_key(root: str, parameter_keys: Collection[str]) -> str | None:
    if root in parameter_keys:
        return root
    return next(
        (
            root[: -len(suffix)]
            for suffix in CREDENTIAL_REAL_VALUE_SUFFIXES
            if root.endswith(suffix) and root[: -len(suffix)] in parameter_keys
        ),
        None,
    )


def keys_named_in(serialized: str, keys: set[str]) -> set[str]:
    named = keys & set(jinja_string_leaves(serialized))
    named.update(key for root in jinja_roots(serialized) if (key := root_parameter_key(root, keys)) is not None)
    return named


def block_read_keys(value: Mapping[str, Any], declared: Collection[str]) -> set[str]:
    """Declared keys the config reads: a string value equal to a key, or a Jinja root naming one."""
    read = {
        key
        for root in jinja_roots(json.dumps(value, default=str))
        if (key := root_parameter_key(root, declared)) is not None
    }
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if item in declared:
                read.add(item)
        elif isinstance(item, Mapping):
            # A natural-language criterion is judged with the whole run context, so it reads every input.
            if item.get("criteria_type") == "prompt":
                return set(declared)
            stack.extend(child for field, child in item.items() if field not in _STRUCTURAL_BLOCK_FIELDS)
        elif isinstance(item, list):
            stack.extend(item)
    return read
