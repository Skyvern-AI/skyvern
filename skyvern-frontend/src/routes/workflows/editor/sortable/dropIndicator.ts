/**
 * Pure helper that maps a (scope order, active, over) tuple to the
 * insertion-line indicator state. Extracted so the index-comparison logic
 * can be unit-tested without mounting DndContext / React Flow.
 *
 * Semantics match `arrayMove(items, oldIndex, newIndex)`:
 *   - active < over → moving downward → indicator "below" over (insert after)
 *   - active > over → moving upward   → indicator "above" over (insert before)
 *   - active === over → null (drop-on-self is a no-op)
 *   - either id missing from `order` → null (cross-scope hover or stale id)
 */
export type DropIndicatorState = {
  overId: string;
  placement: "above" | "below";
} | null;

export function deriveDropIndicator({
  order,
  activeId,
  overId,
}: {
  order: Array<string>;
  activeId: string;
  overId: string;
}): DropIndicatorState {
  if (activeId === overId) return null;
  const oldIndex = order.indexOf(activeId);
  const newIndex = order.indexOf(overId);
  if (oldIndex < 0 || newIndex < 0) return null;
  return {
    overId,
    placement: oldIndex < newIndex ? "below" : "above",
  };
}
