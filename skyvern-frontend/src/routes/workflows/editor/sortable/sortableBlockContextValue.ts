import { createContext } from "react";

/**
 * context payload + default used by the `withSortableBlock` HOC
 * and the `useSortableBlockContext` hook. Split out from
 * `SortableBlockContext.tsx` so the component file exports only the provider
 * component (react-refresh HMR boundary) and the hook file exports only the
 * hook.
 */

type DragListener = (event: React.SyntheticEvent) => void;

type SortableBlockListeners = Record<string, DragListener> | undefined;

// Aria attributes that dnd-kit's `useSortable` returns alongside `listeners`.
// Forwarding them through context lets `NodeGripHandle` spread them onto the
// real focusable button — without it, screen readers don't pick up the
// `aria-describedby` link to the `SCREEN_READER_INSTRUCTIONS` hidden by
// `DndContext` or the `aria-roledescription="sortable"` role hint.
type SortableBlockAttributes =
  | {
      "aria-describedby"?: string;
      "aria-roledescription"?: string;
    }
  | undefined;

type SortableBlockValue = {
  listeners: SortableBlockListeners;
  attributes: SortableBlockAttributes;
  isDragging: boolean;
};

const SortableBlockContext = createContext<SortableBlockValue>({
  listeners: undefined,
  attributes: undefined,
  isDragging: false,
});

export { SortableBlockContext };
export type {
  SortableBlockAttributes,
  SortableBlockListeners,
  SortableBlockValue,
};
