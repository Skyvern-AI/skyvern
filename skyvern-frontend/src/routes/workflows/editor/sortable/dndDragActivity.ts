import { create } from "zustand";

// React Flow tracks pointer drags via `node.dragging`, but dnd-kit's
// keyboard sensor (Space-to-pick / arrows-to-move) doesn't flip that flag.
// History navigation (undo/redo) and captureImmediately must bail while a
// dnd-kit drag is live too, otherwise Cmd/Ctrl+Z mid-drag pops a snapshot
// while DragOverlay still holds the activeDragId, desyncing the two.
type DndDragActivityState = {
  activeDragId: string | null;
  setActiveDragId: (id: string | null) => void;
};

export const useDndDragActivityStore = create<DndDragActivityState>((set) => ({
  activeDragId: null,
  setActiveDragId: (id) => set({ activeDragId: id }),
}));

export function isDndDragInFlight(): boolean {
  return useDndDragActivityStore.getState().activeDragId !== null;
}
