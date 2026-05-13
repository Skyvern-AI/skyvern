"""Deterministic element-tree compression for Skyvern prompts (SKY-9718, Layer 1).

The verifier (`check-user-goal`) and other element-rendering prompts ship a lot
of bytes that aren't needed for the LLM's decision. This module exposes three
independent transforms that callers of `load_prompt_with_elements` can compose
per template:

    transform                             saving (offline analysis, 729 prompts)
    ------------------------------------- --------------------------------------
    1. compress_long_href                  7.2%  — long hrefs → `"#templated"`
    2. compress_image_src                  4.3%  — drops `<img src="…">`
    3. strip_url_query_strings             0.9%  — strips `?…` from href/src

Combined potential ~12% planner savings. The largest single source of bloat
in offline analysis was Skyvern internal IDs (~24% of the verifier prompt),
but that is already addressable through `html_need_skyvern_attrs=False` in
`build_*_elements_tree` — no separate lean flag needed. Callers that don't
want Skyvern internal IDs in the rendered HTML pass `html_need_skyvern_attrs=False`
alongside the lean flags.

The transforms operate on the JSON element tree (a list of element dicts) BEFORE
it gets rendered to HTML by `json_to_html`. This mirrors `_process_element_for_economy_tree`
in `scraped_page.py` — same shape, different per-node logic.

`apply_lean_to_tree` deep-copies the input and is safe to share-by-reference.
"""

from __future__ import annotations

import copy
from typing import Any

# Hashed-href tokens are written by json_to_html when href > 150 chars. We
# replace long hrefs with a short marker BEFORE rendering so json_to_html's
# hash substitution never runs on lean trees.
_HASHED_HREF_PLACEHOLDER = "#templated"
_HASHED_HREF_LEN_THRESHOLD = 150


def _transform_node(
    node: dict,
    *,
    compress_long_href: bool,
    compress_image_src: bool,
    strip_url_query_strings: bool,
) -> dict | None:
    """Apply the selected lean transforms to a single node and recurse into children.

    Mutates `node` in place (caller passes a deep-copied tree). Returns the
    transformed node, or None if the node should be dropped entirely (none of
    the current transforms drop nodes, but the return shape mirrors
    `_process_element_for_economy_tree`).
    """
    tag = (node.get("tagName") or "").lower()
    attributes: dict[str, Any] = node.get("attributes") or {}

    # Ordering matters when both `compress_long_href` and `strip_url_query_strings`
    # are on: strip first so a `https://x.co/foo?<200 chars of utm>` URL collapses
    # to a short `https://x.co/foo` and skips the length check, instead of being
    # blanket-replaced with `#templated` and losing the destination signal.

    # 1. Strip URL query strings from href / src.
    if strip_url_query_strings:
        for key in ("href", "src"):
            val = attributes.get(key)
            if isinstance(val, str) and "?" in val:
                attributes[key] = val.split("?", 1)[0]

    # 2. Compress image src — drop entirely; keep alt + id + everything else.
    if compress_image_src and tag == "img":
        attributes.pop("src", None)

    # 3. Compress long hrefs (would otherwise become `{{_<sha256>}}` in
    # json_to_html). Runs after query-strip so URLs that were only "long" due
    # to tracking junk keep their meaningful path; URLs that are long even
    # without the query (signed CDN, encoded payloads) still get hashed.
    if compress_long_href:
        href = attributes.get("href")
        if isinstance(href, str) and len(href) > _HASHED_HREF_LEN_THRESHOLD:
            attributes["href"] = _HASHED_HREF_PLACEHOLDER

    node["attributes"] = attributes

    children = node.get("children")
    if children:
        new_children: list[dict] = []
        for child in children:
            transformed = _transform_node(
                child,
                compress_long_href=compress_long_href,
                compress_image_src=compress_image_src,
                strip_url_query_strings=strip_url_query_strings,
            )
            if transformed is not None:
                new_children.append(transformed)
        node["children"] = new_children

    return node


def apply_lean_to_tree(
    elements: list[dict],
    *,
    compress_long_href: bool = False,
    compress_image_src: bool = False,
    strip_url_query_strings: bool = False,
) -> list[dict]:
    """Apply the deterministic lean-tree recipe to a list of element dicts.

    Each transform is independently gated. Deep-copies the input; returns a
    new list of transformed element dicts. With every flag False the result is
    a deep copy of the input (no transforms applied).
    """
    out: list[dict] = []
    for element in elements:
        copied = copy.deepcopy(element)
        transformed = _transform_node(
            copied,
            compress_long_href=compress_long_href,
            compress_image_src=compress_image_src,
            strip_url_query_strings=strip_url_query_strings,
        )
        if transformed is not None:
            out.append(transformed)
    return out
