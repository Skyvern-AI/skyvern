import { type ReactNode } from "react";

import {
  SortableBlockContext,
  type SortableBlockValue,
} from "./sortableBlockContextValue";

/**
 * drag-activation wiring.
 *
 * The `withSortableBlock` HOC calls `useSortable({ id })` on every AppNode
 * registered in `nodeTypes`. That hook returns a `listeners` map (pointerdown
 * / keydown / etc.) that must be attached to the element the user actually
 * grabs — the grip handle — not the node root. Prop-drilling those listeners
 * through every node component + `NodeHeader` would require a signature
 * change in ~26 call sites; instead the HOC exposes them via this context,
 * and `NodeGripHandle` reads them directly.
 *
 * `isDragging` is fed through the same context so the grip handle can style
 * itself (cursor, shade) without the node components plumbing the signal
 * themselves. Existing call sites in `NodeHeader` still pass `isDragging` as
 * a prop for unit-test ergonomics — the context value just supplements when
 * the prop is absent or false.
 *
 * The `createContext` call and the `useSortableBlockContext` hook live in a
 * sibling module (`sortableBlockContextValue.ts` / `useSortableBlockContext.ts`)
 * so this file can export only the React component — keeping `react-refresh`'s
 * HMR boundary clean.
 */

function SortableBlockProvider({
  listeners,
  attributes,
  isDragging,
  children,
}: SortableBlockValue & { children: ReactNode }) {
  return (
    <SortableBlockContext.Provider
      value={{ listeners, attributes, isDragging }}
    >
      {children}
    </SortableBlockContext.Provider>
  );
}

export { SortableBlockProvider };
export type {
  SortableBlockAttributes,
  SortableBlockListeners,
} from "./sortableBlockContextValue";
