/**
 * Screen-reader announcements for workflow block dragging.
 *
 * dnd-kit's defaults announce draggable ids; this editor uses label-driven
 * pickup, move, drop, and cancel messages so assistive-tech users can track
 * blocks by their visible names. Transient drag updates are delivered through
 * a polite live region to avoid interrupting the user mid-drag.
 */
import type { Announcements, ScreenReaderInstructions } from "@dnd-kit/core";

/**
 * Minimal shape the announcements need from each node. The real editor uses
 * the `AppNode` union, but the announcer only ever reaches for `id` and the
 * `label` under `data`; keeping the local type narrow means the unit tests
 * can fixture nodes without importing the node registry.
 */
export type AnnouncementNode = {
  id: string;
  data?: { label?: unknown };
};

/**
 * Static instructions read once when the draggable receives focus. Mirrors
 * the sensor bindings in `dragSensors.ts` — keep the wording in sync with
 * any future changes to the keyboard coordinate getter so screen-reader
 * users are not told about shortcuts that no longer work.
 */
export const SCREEN_READER_INSTRUCTIONS: ScreenReaderInstructions = {
  draggable:
    "To pick up a workflow block, press Space or Enter. " +
    "Use the Up and Down arrow keys to move the block within its chain. " +
    "Press Space or Enter again to drop the block into its new position, " +
    "or press Escape to cancel.",
};

/**
 * Resolve a human-readable label for a draggable id. Falls back to the raw
 * id so the announcement still carries a recognisable token when a block
 * is missing a label (shouldn't happen in production because labels are
 * required — but a silent empty-string announcement is worse than the id).
 */
export function resolveBlockLabel(
  nodes: ReadonlyArray<AnnouncementNode>,
  id: unknown,
): string {
  const idString =
    typeof id === "string" || typeof id === "number" ? String(id) : "";
  if (!idString) return "";
  const node = nodes.find((candidate) => candidate.id === idString);
  const rawLabel = node?.data?.label;
  if (typeof rawLabel === "string" && rawLabel.length > 0) {
    return rawLabel;
  }
  return idString;
}

/**
 * Build the Announcements object consumed by DndContext. Captures the
 * `nodes` array at build time — FlowRenderer calls this on every render
 * (cheap, just closes over the already-realised array) so the announcer
 * always sees the post-reorder chain by the time `onDragEnd` fires.
 *
 * Why not a hook: DndContext owns the subscription lifecycle; we only need
 * a fresh object per render. A pure builder is simpler to unit-test and
 * does not constrain the caller's render path.
 */
export function buildDragAnnouncements(
  nodes: ReadonlyArray<AnnouncementNode>,
): Announcements {
  const labelFor = (id: unknown) => resolveBlockLabel(nodes, id);
  return {
    onDragStart({ active }) {
      return `Picked up workflow block ${labelFor(active.id)}.`;
    },
    onDragOver({ active, over }) {
      const activeLabel = labelFor(active.id);
      if (over) {
        return `Workflow block ${activeLabel} is over ${labelFor(over.id)}.`;
      }
      return `Workflow block ${activeLabel} is no longer over a drop target.`;
    },
    onDragEnd({ active, over }) {
      const activeLabel = labelFor(active.id);
      if (over) {
        return `Workflow block ${activeLabel} was dropped onto ${labelFor(over.id)}.`;
      }
      return `Workflow block ${activeLabel} was dropped.`;
    },
    onDragCancel({ active }) {
      return `Drag cancelled. Workflow block ${labelFor(active.id)} returned to its original position.`;
    },
  };
}
