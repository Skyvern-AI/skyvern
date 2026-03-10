"""CDP-based accessibility tree integration.

Uses Chrome DevTools Protocol Accessibility.getFullAXTree to augment
DOM elements with semantic information (roles, accessible names, states)
from the browser's accessibility tree.

This provides semantic information about element purpose that raw DOM
attributes don't capture, especially useful for:
- Custom components (Web Components) with meaningful ARIA roles
- Complex form widgets where the accessible name describes the field's purpose
- Navigation patterns where ARIA landmarks indicate page structure

Inspired by Stagehand's accessibility tree integration.
"""

from __future__ import annotations

import structlog
from playwright.async_api import Page

LOG = structlog.get_logger()


async def get_accessibility_tree_data(page: Page) -> dict[int, AccessibilityNodeInfo]:
    """Fetch the full accessibility tree and return a mapping of backend node IDs to a11y info.

    Returns:
        Dict mapping backend_node_id -> AccessibilityNodeInfo with role, name, and states.
    """
    try:
        cdp_session = await page.context.new_cdp_session(page)
        try:
            result = await cdp_session.send("Accessibility.getFullAXTree")
        finally:
            await cdp_session.detach()
    except Exception:
        LOG.warning("Failed to fetch accessibility tree via CDP", exc_info=True)
        return {}

    return _parse_ax_tree(result)


class AccessibilityNodeInfo:
    """Parsed accessibility node information."""

    __slots__ = ("role", "name", "description", "value", "states")

    def __init__(
        self,
        role: str = "",
        name: str = "",
        description: str = "",
        value: str = "",
        states: dict[str, bool] | None = None,
    ):
        self.role = role
        self.name = name
        self.description = description
        self.value = value
        self.states = states or {}

    def to_annotation(self) -> str:
        """Generate a concise annotation string for LLM consumption."""
        parts = []
        if self.role and self.role not in ("generic", "none", "InlineTextBox"):
            parts.append(f"role={self.role}")
        if self.name:
            parts.append(f'name="{self.name}"')
        if self.description:
            parts.append(f'desc="{self.description}"')

        # Include relevant states
        for state, active in self.states.items():
            if active and state in ("disabled", "expanded", "selected", "checked", "required"):
                parts.append(state)

        return ", ".join(parts)


def _parse_ax_tree(result: dict) -> dict[int, AccessibilityNodeInfo]:
    """Parse the CDP AX tree response into a backend_node_id -> info mapping."""
    mapping: dict[int, AccessibilityNodeInfo] = {}

    nodes = result.get("nodes", [])
    for node in nodes:
        backend_id = node.get("backendDOMNodeId")
        if backend_id is None:
            continue

        role_data = node.get("role", {})
        role = role_data.get("value", "")

        # Skip generic/structural roles that don't add information
        if role in ("generic", "none", "InlineTextBox", "RootWebArea"):
            continue

        name_data = node.get("name", {})
        name = name_data.get("value", "")

        description_data = node.get("description", {})
        description = description_data.get("value", "")

        value_data = node.get("value", {})
        value = str(value_data.get("value", "")) if value_data else ""

        # Parse properties into states
        states: dict[str, bool] = {}
        for prop in node.get("properties", []):
            prop_name = prop.get("name", "")
            prop_value = prop.get("value", {}).get("value")
            if isinstance(prop_value, bool):
                states[prop_name] = prop_value

        # Only include nodes with meaningful information
        if role or name or description:
            mapping[backend_id] = AccessibilityNodeInfo(
                role=role,
                name=name,
                description=description,
                value=value,
                states=states,
            )

    return mapping


def augment_elements_with_accessibility(
    elements: list[dict],
    id_to_backend: dict[str, int],
    ax_tree: dict[int, AccessibilityNodeInfo],
) -> list[dict]:
    """Augment scraped elements with accessibility tree data.

    Adds a `data-ui` or `aria-label` attribute with semantic information
    from the accessibility tree when it provides additional context beyond
    what the raw HTML attributes already contain.
    """
    augmented_count = 0

    for element in elements:
        eid = element.get("id", "")
        backend_id = id_to_backend.get(eid)
        if backend_id is None:
            continue

        ax_info = ax_tree.get(backend_id)
        if ax_info is None:
            continue

        attrs = element.get("attributes", {})
        annotation = ax_info.to_annotation()

        if not annotation:
            continue

        # Only add if it provides new information
        existing_label = attrs.get("aria-label", "")
        existing_role = attrs.get("role", "") or attrs.get("aria-role", "")

        has_new_info = False
        if ax_info.role and ax_info.role != existing_role:
            has_new_info = True
        if ax_info.name and ax_info.name != existing_label:
            has_new_info = True

        if has_new_info:
            # Add accessibility annotation via shape-description
            # (already in RESERVED_ATTRIBUTES so it won't be trimmed)
            existing_desc = attrs.get("shape-description", "")
            if existing_desc:
                attrs["shape-description"] = f"{existing_desc}; a11y: {annotation}"
            else:
                attrs["shape-description"] = f"a11y: {annotation}"
            augmented_count += 1

    if augmented_count > 0:
        LOG.info("Accessibility tree augmented elements", augmented_count=augmented_count)

    return elements
