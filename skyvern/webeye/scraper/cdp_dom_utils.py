"""CDP-based DOM utilities for enhanced visibility detection.

Uses Chrome DevTools Protocol DOMSnapshot.captureSnapshot to determine actual
paint order and element occlusion. This is superior to JavaScript-based visibility
detection because it uses the browser's actual compositing information, catching:
- Elements hidden by z-index stacking (modals, overlays, dropdown menus)
- Elements clipped by CSS `overflow: hidden` on ancestors
- Elements occluded by CSS transforms, filters, and complex compositing
- Elements with pointer-events: none applied via complex CSS rules

Inspired by Browser-use's paint-order filtering approach.
"""

from __future__ import annotations

import structlog
from playwright.async_api import Page

LOG = structlog.get_logger()


async def get_paint_order_occluded_backend_ids(page: Page) -> set[int]:
    """Use CDP DOMSnapshot to identify elements that are visually occluded.

    Returns a set of backend node IDs for elements that are painted over by
    other elements (i.e., they exist in the DOM but aren't visually accessible).
    """
    try:
        cdp_session = await page.context.new_cdp_session(page)
        try:
            snapshot = await cdp_session.send(
                "DOMSnapshot.captureSnapshot",
                {
                    "computedStyles": ["visibility", "display", "opacity", "pointer-events"],
                    "includePaintOrder": True,
                    "includeDOMRects": True,
                },
            )
        finally:
            await cdp_session.detach()
    except Exception:
        LOG.warning("Failed to capture CDP DOMSnapshot for paint-order filtering", exc_info=True)
        return set()

    return _compute_occluded_ids(snapshot)


def _compute_occluded_ids(snapshot: dict) -> set[int]:
    """Analyze DOMSnapshot data to find occluded elements.

    Elements are considered occluded if another element with a higher paint order
    overlaps their bounding rect significantly (>50% overlap).
    """
    occluded: set[int] = set()
    documents = snapshot.get("documents", [])

    for doc in documents:
        layout = doc.get("layout", {})
        node_indices = layout.get("nodeIndex", [])
        bounds_list = layout.get("bounds", [])
        paint_orders = layout.get("paintOrders", [])
        backend_node_ids = doc.get("nodes", {}).get("backendNodeId", [])

        if not node_indices or not bounds_list or not paint_orders:
            continue

        # Build list of (paint_order, bounds, backend_node_id) for each layout node
        entries: list[tuple[int, list[float], int]] = []
        for i, node_idx in enumerate(node_indices):
            if i >= len(bounds_list) or i >= len(paint_orders):
                break
            if node_idx >= len(backend_node_ids):
                continue

            bounds = bounds_list[i]
            if len(bounds) < 4:
                continue

            # Skip zero-area elements
            x, y, w, h = bounds[0], bounds[1], bounds[2], bounds[3]
            if w <= 0 or h <= 0:
                continue

            paint_order = paint_orders[i]
            backend_id = backend_node_ids[node_idx]
            entries.append((paint_order, [x, y, w, h], backend_id))

        # Sort by paint order ascending (lower paint order = painted first = behind)
        entries.sort(key=lambda e: e[0])

        # Check each element against higher paint-order elements for occlusion
        for i, (po_i, bounds_i, bid_i) in enumerate(entries):
            x1, y1, w1, h1 = bounds_i
            area_i = w1 * h1
            if area_i <= 0:
                continue

            for j in range(i + 1, len(entries)):
                po_j, bounds_j, bid_j = entries[j]
                if po_j <= po_i:
                    continue

                x2, y2, w2, h2 = bounds_j
                # Compute intersection
                ix = max(x1, x2)
                iy = max(y1, y2)
                ix2 = min(x1 + w1, x2 + w2)
                iy2 = min(y1 + h1, y2 + h2)

                if ix2 > ix and iy2 > iy:
                    overlap_area = (ix2 - ix) * (iy2 - iy)
                    overlap_ratio = overlap_area / area_i
                    # Element is occluded if >50% of its area is covered
                    if overlap_ratio > 0.5:
                        occluded.add(bid_i)
                        break  # No need to check more overlapping elements

    return occluded


async def get_js_event_listeners(page: Page, backend_node_ids: list[int]) -> set[int]:
    """Use CDP to detect elements with JavaScript event listeners.

    Returns a set of backend node IDs that have click/mousedown/mouseup/pointerdown
    event listeners attached. This catches non-semantic interactive elements
    (divs with JS click handlers) that attribute-based detection misses.

    Inspired by Browser-use's JS listener detection.
    """
    interactive_ids: set[int] = set()

    if not backend_node_ids:
        return interactive_ids

    try:
        cdp_session = await page.context.new_cdp_session(page)
        try:
            # Enable DOM domain for resolveNode
            await cdp_session.send("DOM.enable")

            # Check a sample of elements (checking all can be slow)
            # Prioritize checking elements that look non-interactive by attributes
            sample_size = min(len(backend_node_ids), 200)
            for backend_id in backend_node_ids[:sample_size]:
                try:
                    # Resolve the backend node to a remote object
                    result = await cdp_session.send(
                        "DOM.resolveNode",
                        {"backendNodeId": backend_id},
                    )
                    object_id = result.get("object", {}).get("objectId")
                    if not object_id:
                        continue

                    # Get event listeners for this node
                    listeners_result = await cdp_session.send(
                        "DOMDebugger.getEventListeners",
                        {"objectId": object_id, "depth": 0},
                    )
                    listeners = listeners_result.get("listeners", [])

                    # Check for click-like event listeners
                    interactive_event_types = {
                        "click",
                        "mousedown",
                        "mouseup",
                        "pointerdown",
                        "pointerup",
                        "touchstart",
                        "touchend",
                    }
                    for listener in listeners:
                        if listener.get("type") in interactive_event_types:
                            interactive_ids.add(backend_id)
                            break
                except Exception:
                    # Individual node resolution can fail for detached nodes
                    continue
        finally:
            await cdp_session.detach()
    except Exception:
        LOG.warning("Failed to query JS event listeners via CDP", exc_info=True)

    return interactive_ids
