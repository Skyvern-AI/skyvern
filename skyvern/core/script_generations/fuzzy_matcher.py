"""Fuzzy matching for form option labels.

Consolidates the duplicated option-matching logic used throughout the
form-filling pipeline into a single, configurable implementation.
"""

from __future__ import annotations

import re

import structlog

LOG = structlog.get_logger()

_WORD_RE = re.compile(r"\w+")


_NORMALIZE_RE = re.compile(r"[''`]")


def _normalize(text: str) -> str:
    """Normalize text for matching: lowercase, strip apostrophes/quotes."""
    return _NORMALIZE_RE.sub("", text.lower().strip())


def match_option(candidate: str, options: list[str], *, min_substring_len: int = 3) -> int | None:
    """Find the best matching option index for a candidate value.

    Uses a 4-pass strategy:
      1. Exact match (case-insensitive, apostrophe-normalized)
      2. Substring containment (candidate in option or option in candidate)
      3. Stem match (e.g., "bachelors" matches "bachelor's degree")
      4. Word overlap scoring (Jaccard-like, threshold >= 50%)

    Args:
        candidate: The value to match (e.g., "Bachelor of Science").
        options: List of option labels to match against.
        min_substring_len: Minimum length for substring matches to avoid
            false positives on very short strings.

    Returns:
        Index of the best matching option, or None if no match found.
    """
    if not candidate or not options:
        return None

    candidate_norm = _normalize(candidate)

    # Pass 1: exact match (normalized)
    for i, opt in enumerate(options):
        if _normalize(opt) == candidate_norm:
            return i

    # Pass 2: substring containment (normalized, both directions)
    best_sub_idx: int | None = None
    best_sub_len = 0
    for i, opt in enumerate(options):
        opt_norm = _normalize(opt)
        if len(candidate_norm) >= min_substring_len and candidate_norm in opt_norm:
            if len(candidate_norm) > best_sub_len:
                best_sub_len = len(candidate_norm)
                best_sub_idx = i
        elif len(opt_norm) >= min_substring_len and opt_norm in candidate_norm:
            if len(opt_norm) > best_sub_len:
                best_sub_len = len(opt_norm)
                best_sub_idx = i
    if best_sub_idx is not None:
        return best_sub_idx

    # Pass 3: stem match — strip trailing 's' and compare
    # Catches: "bachelors" ↔ "bachelor's", "masters" ↔ "master's"
    candidate_stem = candidate_norm.rstrip("s")
    if len(candidate_stem) >= min_substring_len:
        for i, opt in enumerate(options):
            opt_stem = _normalize(opt).rstrip("s")
            if candidate_stem in opt_stem or opt_stem in candidate_stem:
                return i

    # Pass 4: word overlap scoring
    candidate_words = set(_WORD_RE.findall(candidate_norm))
    if not candidate_words:
        return None

    best_overlap_idx: int | None = None
    best_overlap_score = 0.0
    for i, opt in enumerate(options):
        opt_words = set(_WORD_RE.findall(_normalize(opt)))
        if not opt_words:
            continue
        overlap = len(candidate_words & opt_words)
        score = overlap / max(len(candidate_words), len(opt_words))
        if score > best_overlap_score:
            best_overlap_score = score
            best_overlap_idx = i

    if best_overlap_score >= 0.5 and best_overlap_idx is not None:
        return best_overlap_idx

    return None
