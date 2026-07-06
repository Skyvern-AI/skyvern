import { PinBottomIcon, PinTopIcon } from "@radix-ui/react-icons";
import {
  getNodesBounds,
  Panel,
  useReactFlow,
  useStore,
  type ReactFlowState,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";

import { ControlTooltip } from "../../studio/ControlTooltip";
import type { AppNode } from "../nodes";
import {
  endAnchoredViewport,
  flowJumpVisibility,
  paneRefitDuration,
  startAnchoredViewport,
} from "../paneFit";

const selectWidth = (state: ReactFlowState) => state.width;
const selectHeight = (state: ReactFlowState) => state.height;
const selectViewportX = (state: ReactFlowState) => state.transform[0];
const selectViewportY = (state: ReactFlowState) => state.transform[1];
const selectZoom = (state: ReactFlowState) => state.transform[2];
const selectDomNode = (state: ReactFlowState) => state.domNode;

const JUMP_CONTROLS_GAP_PX = 8;

const jumpButtonClass =
  "h-7 w-7 border-border bg-background/70 text-muted-foreground backdrop-blur hover:bg-background/90 hover:text-foreground";

/**
 * Contextual jump-to-start / jump-to-end buttons for long flows, sharing the
 * canvas's left rail (the zoom/fit Controls' vertical axis): jump-to-start at
 * the top-left corner, jump-to-end directly above the Controls stack. Each
 * shows only while its end of the flow is scrolled out of view, and both hide
 * when the whole flow fits the pane. The jumps land on the same start/end-
 * anchored viewports the studio pane-fit policy uses, so a jump reads like
 * the initial load anchored at that end.
 */
export function FlowJumpControls({ nodes }: { nodes: Array<AppNode> }) {
  const reactFlowInstance = useReactFlow();
  const paneWidth = useStore(selectWidth);
  const paneHeight = useStore(selectHeight);
  const viewportX = useStore(selectViewportX);
  const viewportY = useStore(selectViewportY);
  const zoom = useStore(selectZoom);
  const domNode = useStore(selectDomNode);

  // Bottom inset that keeps the jump-to-end button stacked above the
  // Controls panel with a constant gap. Measured (not a constant) because
  // the stack's height changes at runtime: the undo/redo controls collapse
  // while their history is empty.
  const [controlsClearancePx, setControlsClearancePx] = useState<number | null>(
    null,
  );
  useEffect(() => {
    if (!domNode) {
      setControlsClearancePx(null);
      return;
    }
    const controls = domNode.querySelector(".react-flow__controls");
    if (!controls) {
      setControlsClearancePx(null);
      return;
    }
    const update = () => {
      // Distance from the pane bottom to the stack's top edge; folds in the
      // panel's own bottom margin so the button needs no margin constant.
      const clearance =
        domNode.getBoundingClientRect().bottom -
        controls.getBoundingClientRect().top +
        JUMP_CONTROLS_GAP_PX;
      setControlsClearancePx(clearance > 0 ? clearance : null);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(controls);
    return () => {
      observer.disconnect();
    };
  }, [domNode]);

  const bounds = useMemo(() => {
    const visibleNodes = nodes.filter((node) => !node.hidden);
    return visibleNodes.length > 0 ? getNodesBounds(visibleNodes) : null;
  }, [nodes]);

  const { showJumpToStart, showJumpToEnd } =
    bounds === null
      ? { showJumpToStart: false, showJumpToEnd: false }
      : flowJumpVisibility({
          pane: { width: paneWidth, height: paneHeight },
          viewport: { x: viewportX, y: viewportY, zoom },
          bounds,
        });

  const jumpTo = useCallback(
    (anchoredViewport: typeof startAnchoredViewport) => {
      const visibleNodes = reactFlowInstance
        .getNodes()
        .filter((node) => !node.hidden);
      if (visibleNodes.length === 0) {
        return;
      }
      const viewport = anchoredViewport({
        pane: { width: paneWidth, height: paneHeight },
        bounds: getNodesBounds(visibleNodes),
      });
      if (viewport === null) {
        return;
      }
      reactFlowInstance.setViewport(viewport, {
        duration: paneRefitDuration(),
      });
    },
    [reactFlowInstance, paneWidth, paneHeight],
  );

  return (
    <>
      {showJumpToStart && (
        <Panel position="top-left">
          <ControlTooltip content="Jump to start" side="right">
            <Button
              variant="outline"
              size="icon"
              className={jumpButtonClass}
              aria-label="Jump to start"
              onClick={() => jumpTo(startAnchoredViewport)}
            >
              <PinTopIcon className="h-4 w-4" />
            </Button>
          </ControlTooltip>
        </Panel>
      )}
      {showJumpToEnd && (
        <Panel
          position="bottom-left"
          style={
            controlsClearancePx === null
              ? undefined
              : { marginBottom: controlsClearancePx }
          }
        >
          <ControlTooltip content="Jump to end" side="right">
            <Button
              variant="outline"
              size="icon"
              className={jumpButtonClass}
              aria-label="Jump to end"
              onClick={() => jumpTo(endAnchoredViewport)}
            >
              <PinBottomIcon className="h-4 w-4" />
            </Button>
          </ControlTooltip>
        </Panel>
      )}
    </>
  );
}
