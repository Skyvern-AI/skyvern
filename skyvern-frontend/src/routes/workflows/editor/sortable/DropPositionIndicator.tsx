import { useStore, type ReactFlowState } from "@xyflow/react";

import type { DropIndicatorState } from "./dropIndicator";

type DropPositionIndicatorProps = {
  state: DropIndicatorState;
};

type Rect = { left: number; top: number; width: number; height: number };

/**
 * Reads the over node's screen rect from React Flow's internal store so the
 * indicator stays glued to the right place even as the user pans / zooms
 * during the drag. Re-renders on viewport changes via the `useStore`
 * subscription rather than on a setInterval.
 *
 * Uses `internals.positionAbsolute` when available so nested blocks (loop
 * children, conditional-branch children) position correctly — their
 * `position` is relative to the parent, but the viewport transform applies
 * to the absolute position.
 */
function useNodeScreenRect(nodeId: string | undefined): Rect | null {
  const internalNode = useStore((s: ReactFlowState) =>
    nodeId ? s.nodeLookup.get(nodeId) : undefined,
  );
  const transform = useStore((s: ReactFlowState) => s.transform);

  if (!internalNode) return null;
  const measured = internalNode.measured;
  const width = measured?.width ?? 0;
  const height = measured?.height ?? 0;
  if (!width || !height) return null;

  const absolute =
    internalNode.internals?.positionAbsolute ?? internalNode.position;
  const [tx, ty, tz] = transform;
  // Cap the indicator width to the inner block card (~30rem) so loop /
  // conditional containers (which measure dashed-border + child area)
  // do not bleed the 2px line past the visible block edge. Pre-zoom in
  // CSS px; the viewport zoom factor still applies after the clamp.
  const INNER_BLOCK_MAX_PX = 30 * 16;
  const clampedWidthPx = Math.min(width, INNER_BLOCK_MAX_PX);
  return {
    left: absolute.x * tz + tx,
    top: absolute.y * tz + ty,
    width: clampedWidthPx * tz,
    height: height * tz,
  };
}

/**
 * Horizontal 2 px line anchored to the over block's top or bottom edge to
 * show where the dragged block will land on drop. Pointer-events off so it
 * cannot interfere with the ongoing drag hit-test. Rendered as a sibling of
 * `<ReactFlow>` against FlowRenderer's `position: relative` outer wrapper so
 * RF's node DOM does not clip it.
 */
export function DropPositionIndicator({ state }: DropPositionIndicatorProps) {
  const rect = useNodeScreenRect(state?.overId);
  if (!state || !rect) return null;

  const lineTop =
    state.placement === "above" ? rect.top - 1 : rect.top + rect.height - 1;

  return (
    <div
      data-testid="drop-position-indicator"
      data-placement={state.placement}
      data-over-id={state.overId}
      aria-hidden="true"
      style={{
        position: "absolute",
        left: rect.left,
        top: lineTop,
        width: rect.width,
        height: 2,
        pointerEvents: "none",
        zIndex: 50,
      }}
      className="rounded-full bg-blue-500 shadow-[0_0_6px_rgba(59,130,246,0.6)]"
    />
  );
}
