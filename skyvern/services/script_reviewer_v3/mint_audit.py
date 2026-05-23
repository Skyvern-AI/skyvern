"""Mint-time static audit for freshly-generated cached scripts.

The post-run reviewer (``v3_review_post_run``) is reactive — it only fires
when the cached script produces an episode at runtime. That means a script
whose selectors are obviously hardcoded from cold-mint scrape data won't
get reviewed until something else trips an episode, which might be never
(``ai='fallback'`` masks the miss).

This module runs synchronously after cold-mint, on the generated source,
WITHOUT calling an LLM. It uses the static validators to look for literals
that look like baked-in runtime data (scraped IDs, paper numbers, product
SKUs) and emits structured findings.

The trigger contract (Policy A, see commit message):

- audit returns ``[]``: no v3 agent loop fires — the script is fine.
- audit returns 1+ findings: caller is expected to fire ``v3_review_post_run``
  with the findings packed into the context, so the agent loop runs exactly
  once at mint time to propose a fix.

Net: v3 LLM cost is paid only on cold mints that look suspicious, not on
every run. Healthy mints cost nothing.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Iterable

import structlog

LOG = structlog.get_logger()


# A literal must be at least this many characters before we'll flag it.
# Shorter literals (3 chars or fewer) are almost always structural (CSS
# selectors like 'btn', 'a', 'h1') and the false-positive rate isn't worth
# the signal.
_MIN_LITERAL_LENGTH = 4


# Match the value inside a `selector=` kwarg of either page.click() /
# page.input_text() / page.fill_autocomplete() / page.select_option(),
# in either single or double quotes. Captures the inner string verbatim
# (including any escaped quotes the codegen left in place).
_SELECTOR_KWARG_SINGLE_RE = re.compile(r"""\bselector\s*=\s*f?'([^'\\]*(?:\\.[^'\\]*)*)'""")
_SELECTOR_KWARG_DOUBLE_RE = re.compile(r'''\bselector\s*=\s*f?"([^"\\]*(?:\\.[^"\\]*)*)"''')

# Extract candidate identifiers from inside selector strings. Targets:
#   :has-text("X")       → X
#   :has-text('X')       → X
#   [data-foo="X"]       → X
#   [data-foo='X']       → X
#   #some-id             → some-id
#   text="X"             → X
#   text='X'             → X
# Generic CSS structural tokens (a, button, div, .class-name without an
# id-looking value) are NOT extracted — they're never specific runtime data.
_HAS_TEXT_RE = re.compile(r""":has-text\(\s*['"]([^'"]+)['"]\s*\)""")
# Capture (attribute_name, value) so the caller can skip page-structural
# attribute names (aria-label, role, etc.) which are NOT scraped runtime data.
_ATTR_VALUE_RE = re.compile(r"""\[(\w[\w-]*)\s*[*~|^$]?=\s*['"]([^'"]+)['"]\]""")
_ID_RE = re.compile(r"""#([\w][\w-]*)""")
_TEXT_KWARG_RE = re.compile(r"""\btext\s*=\s*['"]([^'"]+)['"]""")

# Attribute names whose values are stable page-structure metadata, not
# runtime data scraped during a cold mint. A literal like
# ``aria-label="Search term or terms"`` is the same across every run of a
# site, so flagging it as a "scrape leak" is a false positive — even when
# the literal doesn't appear in the user prompt.
_STRUCTURAL_ATTRIBUTE_NAMES = frozenset(
    {
        "aria-label",
        "aria-labelledby",
        "aria-describedby",
        "aria-controls",
        "role",
        "name",
        "alt",
        "title",
        "placeholder",
        "type",
        "for",
        "rel",
    }
)


@dataclasses.dataclass
class SuspiciousLiteralFinding:
    """One literal in a selector that doesn't trace back to allowed sources.

    Emitted to v3 post-run agent as input context so it can decide whether
    to rewrite the block / propose a parameter / leave it alone.
    """

    type: str  # "suspicious_literal_in_selector"
    literal: str
    selector: str
    reason: str
    # Optional location hint: which file inside the script revision the
    # literal was found in. Helpful when v3 wants to propose a block_edit.
    file_path: str | None = None
    # Block label derived from file_path. ``blocks/Dictionary.skyvern`` →
    # ``"Dictionary"``; ``main.py`` (or None file_path) → ``None``.
    # Set automatically in ``find_suspicious_selector_literals`` so the
    # mint-review agent can dispatch ``persist_block_edit(block_label=...)``
    # directly without re-parsing.
    block_label: str | None = None


_BLOCKS_FILE_PREFIX = "blocks/"
_BLOCK_FILE_SUFFIX = ".skyvern"


def _block_label_from_file_path(file_path: str | None) -> str | None:
    """Recover the block_label from a script file_path.

    ``blocks/Dictionary.skyvern`` → ``"Dictionary"``.
    Anything else (``main.py``, None, malformed) → ``None``.
    """
    if not file_path or not file_path.startswith(_BLOCKS_FILE_PREFIX):
        return None
    label = file_path[len(_BLOCKS_FILE_PREFIX) :]
    if label.endswith(_BLOCK_FILE_SUFFIX):
        label = label[: -len(_BLOCK_FILE_SUFFIX)]
    return label or None


# Normalize delimiters before substring-matching so that prompts written
# with spaces still match literals written with hyphens or underscores
# (and vice versa). Without this, ``Quantum-Computing-Papers`` fails to
# match a prompt that says ``quantum computing papers`` — a known
# false-positive class from the live test on 2026-05-12.
_DELIMITER_RE = re.compile(r"[-_\s]+")


def _normalize_for_match(value: str) -> str:
    """Lowercase and collapse runs of ``-`` / ``_`` / whitespace into a
    single space. Used for substring comparison only — the original literal
    is preserved in the emitted finding."""
    return _DELIMITER_RE.sub(" ", value.lower()).strip()


def _extract_literals_from_selector(selector: str) -> list[str]:
    """Pull candidate identifiers out of a CSS / Playwright selector string.

    Returns identifiers that could be runtime-data leaks: text content,
    attribute values, ID names. Skips generic structural tokens.
    """
    results: list[str] = []
    for m in _HAS_TEXT_RE.finditer(selector):
        results.append(m.group(1))
    for m in _ATTR_VALUE_RE.finditer(selector):
        attr_name, attr_value = m.group(1), m.group(2)
        # Skip page-structural attributes — their values are stable site
        # metadata (page template content), not scraped runtime data.
        if attr_name.lower() in _STRUCTURAL_ATTRIBUTE_NAMES:
            continue
        results.append(attr_value)
    for m in _TEXT_KWARG_RE.finditer(selector):
        results.append(m.group(1))
    # ID selectors are extracted last so we only look at them if the more
    # specific patterns above didn't already cover this selector.
    for m in _ID_RE.finditer(selector):
        candidate = m.group(1)
        # Skip well-known framework prefixes that are noise (Ember, React
        # Select, etc.) — those are flagged by the fragile-selector
        # validator separately.
        if not _looks_structural(candidate):
            results.append(candidate)
    return results


def _looks_structural(token: str) -> bool:
    """Heuristic: does this look like a generic structural token (e.g. 'btn',
    'main-content') rather than runtime data (e.g. ':scraped-id-2605860')?

    Used to suppress false positives on common CSS-id naming. The rule:
    tokens that are kebab-case English words with no digits are usually
    structural; tokens with mixed alphanumeric + punctuation are usually
    runtime data.
    """
    if any(ch.isdigit() for ch in token):
        return False
    if ":" in token or "/" in token:
        return False
    return True


def _find_all_selectors(source: str) -> list[str]:
    """Find every selector= kwarg value in the source. Order-preserving."""
    out: list[str] = []
    for m in _SELECTOR_KWARG_SINGLE_RE.finditer(source):
        out.append(m.group(1))
    for m in _SELECTOR_KWARG_DOUBLE_RE.finditer(source):
        out.append(m.group(1))
    return out


def find_suspicious_selector_literals(
    *,
    source: str,
    allowed_strings: Iterable[str],
    file_path: str | None = None,
) -> list[SuspiciousLiteralFinding]:
    """Find selector literals that don't trace back to allowed sources.

    The "allowed sources" bundle (built by the caller) is the union of:
      - the user's original task prompt
      - declared workflow parameters (names + default values)
      - outputs of any upstream blocks in the same workflow

    A selector literal is flagged when its content does not appear, as a
    case-insensitive substring, anywhere in that bundle. Empty allowed_strings
    short-circuits to ``[]`` — without an envelope we have no way to judge,
    so silence is correct.
    """
    allowed_norm = [_normalize_for_match(s) for s in allowed_strings if s]
    if not allowed_norm:
        return []

    block_label = _block_label_from_file_path(file_path)
    findings: list[SuspiciousLiteralFinding] = []
    seen: set[tuple[str, str]] = set()
    for selector in _find_all_selectors(source):
        for literal in _extract_literals_from_selector(selector):
            if len(literal) < _MIN_LITERAL_LENGTH:
                continue
            lit_norm = _normalize_for_match(literal)
            if any(lit_norm in allowed for allowed in allowed_norm):
                continue
            key = (literal, selector)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                SuspiciousLiteralFinding(
                    type="suspicious_literal_in_selector",
                    literal=literal,
                    selector=selector,
                    reason=(
                        "literal not found in user prompt, declared workflow parameters, or upstream block outputs"
                    ),
                    file_path=file_path,
                    block_label=block_label,
                )
            )
    return findings


def build_allowed_strings_envelope(
    *,
    user_prompt: str | None,
    workflow_parameters: dict[str, Any] | None,
    upstream_block_outputs: dict[str, Any] | None,
) -> list[str]:
    """Bundle every string the caller can legitimately bake into a selector.

    The validator considers a selector literal "OK" if its content appears
    inside any of the strings returned here. Each input is flattened to a
    list of human-readable strings; nested dicts / lists are stringified
    so deeper structure doesn't hide allowed values.

    Caller-side ergonomics: this never raises on None / missing inputs —
    those just contribute nothing to the envelope. An empty envelope
    short-circuits the validator entirely.
    """
    strings: list[str] = []
    if user_prompt:
        strings.append(user_prompt)
    if workflow_parameters:
        for key, value in workflow_parameters.items():
            strings.append(str(key))
            if value is not None:
                strings.append(str(value))
    if upstream_block_outputs:
        for key, value in upstream_block_outputs.items():
            strings.append(str(key))
            if value is not None:
                # Stringify whole output — substring match handles nested
                # keys / list items / etc.
                strings.append(str(value))
    return strings


__all__ = [
    "SuspiciousLiteralFinding",
    "build_allowed_strings_envelope",
    "find_suspicious_selector_literals",
]
