import { useReactFlow } from "@xyflow/react";
import { useLayoutEffect } from "react";

import { AppNode } from "./nodes";

/**
 * Some facts:
 *   - the workflow editor is rendered as an infinite canvas
 *   - nodes in the editor can have text fields
 *   - users type in those text fields
 *   - the browser will automatically attempt to scroll to a caret position when
 *     the caret leaves the viewport, because it is Being A Good Browser(tm)
 *   - this causes layout artifacts on the infinite canvas
 *
 * `useAutoPan` detects when the viewport is scrolled. (But it should never have
 * a scroll, as it is an infinite canvas!) If a scroll value is detected, then
 * we pan the viewport to counteract the scroll, and set the scroll to 0.
 *
 * The end result is that:
 *   - if a user is typing in any textual HTML element, and they scroll beyond
 *     the viewport, the viewport will animate-pan to counteract the scroll
 *   - if the user pastes large amounts of text into any textual HTML element,
 *     the viewport will animate-pan to counteract the scroll
 *
 * `editorElementRef`: a ref to the top-level editor element (the top-level div
 * for react-flow, at time of writing)
 *
 * `nodes`: `AppNode`s; but could be anything that carries state indicative of a
 * change in the editor
 */
const useAutoPan = (
  editorElementRef: React.RefObject<HTMLDivElement>,
  nodes: AppNode[],
) => {
  const { setViewport, getViewport } = useReactFlow();

  useLayoutEffect(() => {
    const editorElement = editorElementRef.current;
    if (editorElement) {
      const scrollTop = editorElement.scrollTop;

      if (scrollTop === 0) {
        return;
      }

      editorElement.scrollTop = 0;
      const { x, y, zoom } = getViewport();
      const panAmount = editorElement.clientHeight * 0.3;

      setViewport(
        { x, y: y - (scrollTop + panAmount), zoom },
        { duration: 300 },
      );
    }
  }, [nodes, editorElementRef, setViewport, getViewport]);
};

export { useAutoPan };
