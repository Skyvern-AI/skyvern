import { useEffect, useRef, useState, type ReactNode } from "react";

import { cn } from "@/util/utils";

import {
  clampSplitFraction,
  DEFAULT_SPLIT_FRACTION,
  DIVIDER_HEIGHT_PX,
  DIVIDER_KEY_STEP_PX,
  gridTemplateRowsFor,
  persistFraction,
  readStoredFraction,
} from "./resizableTimelineSplitMath";

/**
 * Vertical split between two stacked panes (run timeline above block detail)
 * with a draggable divider that is exactly the old inter-pane gap —
 * invisible at rest, visible on hover/focus/drag. Mirrors StudioPaneDivider's
 * pointer-capture idiom (studio/StudioShell.tsx), generalized from per-pane
 * pixel widths to a single 0..1 fraction shared by exactly two panes.
 */
export function ResizableTimelineSplit({
  top,
  bottom,
  className,
}: {
  top: ReactNode;
  bottom: ReactNode;
  className?: string;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [fraction, setFraction] = useState(readStoredFraction);
  const [active, setActive] = useState(false);
  const dragRef = useRef<{
    pointerId: number;
    startY: number;
    startFraction: number;
    contentHeight: number;
    lastFraction: number;
  } | null>(null);

  // A route change can unmount the divider mid-drag; put back the cursor.
  useEffect(() => {
    return () => {
      if (dragRef.current) {
        dragRef.current = null;
        document.body.style.cursor = "";
      }
    };
  }, []);

  const contentHeight = () => {
    const el = containerRef.current;
    if (!el) return 0;
    return el.getBoundingClientRect().height - DIVIDER_HEIGHT_PX;
  };

  const beginDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || dragRef.current) {
      return;
    }
    const height = contentHeight();
    if (height <= 0) {
      return;
    }
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      startY: event.clientY,
      startFraction: fraction,
      contentHeight: height,
      lastFraction: fraction,
    };
    document.body.style.cursor = "row-resize";
    setActive(true);
  };

  const moveDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || event.pointerId !== drag.pointerId) {
      return;
    }
    const desiredTopPx =
      drag.startFraction * drag.contentHeight + (event.clientY - drag.startY);
    const next = clampSplitFraction(desiredTopPx, drag.contentHeight);
    drag.lastFraction = next;
    if (containerRef.current) {
      containerRef.current.style.gridTemplateRows = gridTemplateRowsFor(next);
    }
  };

  const endDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || event.pointerId !== drag.pointerId) {
      return;
    }
    dragRef.current = null;
    document.body.style.cursor = "";
    setActive(false);
    // A click without a move (or a drag clamped back to where it started)
    // leaves nothing to commit — skip the redundant render + storage write.
    if (drag.lastFraction === drag.startFraction) {
      return;
    }
    setFraction(drag.lastFraction);
    persistFraction(drag.lastFraction);
  };

  const keyResize = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") {
      return;
    }
    event.preventDefault();
    const height = contentHeight();
    if (height <= 0) {
      return;
    }
    const currentTopPx = fraction * height;
    const delta =
      event.key === "ArrowUp" ? -DIVIDER_KEY_STEP_PX : DIVIDER_KEY_STEP_PX;
    const next = clampSplitFraction(currentTopPx + delta, height);
    if (next === fraction) {
      return;
    }
    setFraction(next);
    persistFraction(next);
  };

  const resetSplit = () => {
    setFraction(DEFAULT_SPLIT_FRACTION);
    persistFraction(DEFAULT_SPLIT_FRACTION);
  };

  return (
    <div
      ref={containerRef}
      className={cn("grid min-h-0", className)}
      style={{ gridTemplateRows: gridTemplateRowsFor(fraction) }}
    >
      {top}
      <div
        role="separator"
        aria-orientation="horizontal"
        aria-label="Resize timeline and block details"
        aria-valuenow={Math.round(fraction * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        tabIndex={0}
        title="Drag to resize · double-click to reset · arrow keys to adjust"
        onPointerDown={beginDrag}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onLostPointerCapture={endDrag}
        onDoubleClick={resetSplit}
        onKeyDown={keyResize}
        className="group relative cursor-row-resize touch-none focus-visible:outline-none"
      >
        <span
          aria-hidden
          className={cn(
            "absolute inset-x-3 top-1/2 h-0.5 -translate-y-1/2 rounded-full motion-safe:transition-colors",
            active
              ? "bg-muted-foreground"
              : "bg-transparent group-hover:bg-border group-focus-visible:bg-ring",
          )}
        />
      </div>
      {bottom}
    </div>
  );
}
