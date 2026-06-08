// A container block (loop / conditional) drives a canvas re-layout whenever
// its collapse state flips, because its rendered height changes and every
// block below it must re-stack. The two transitions need different timing:
//
//   - Expanding: children become visible again and the layout recomputes the
//     container height from them, so a synchronous dispatch is enough.
//   - Collapsing: the container shrinks to its header card and React Flow
//     re-measures that smaller height asynchronously. Dispatching synchronously
//     would re-run layout against the stale (expanded) height, leaving the
//     block below stranded at the container's old Y. Wait two animation frames
//     for the new measurement to land before dispatching.
//
// Returns a cleanup that cancels any frames still pending when the effect
// re-runs or the node unmounts.
export function scheduleCollapseRelayout(
  eventName: "conditional-header-resized" | "loop-header-resized",
  prevIsCollapsed: boolean | null,
  isCollapsed: boolean,
): () => void {
  const wasCollapsed = prevIsCollapsed === true;
  const wasExpanded = prevIsCollapsed === false;

  if (wasCollapsed && !isCollapsed) {
    window.dispatchEvent(new Event(eventName));
    return () => {};
  }

  if (wasExpanded && isCollapsed) {
    let innerFrame: number | null = null;
    const outerFrame = requestAnimationFrame(() => {
      innerFrame = requestAnimationFrame(() => {
        window.dispatchEvent(new Event(eventName));
      });
    });
    return () => {
      cancelAnimationFrame(outerFrame);
      if (innerFrame !== null) {
        cancelAnimationFrame(innerFrame);
      }
    };
  }

  return () => {};
}
