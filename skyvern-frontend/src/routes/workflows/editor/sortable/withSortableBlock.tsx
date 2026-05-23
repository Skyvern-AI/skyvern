import { useSortable } from "@dnd-kit/sortable";
import type { NodeProps } from "@xyflow/react";
import type { ComponentType } from "react";

import { SortableBlockProvider } from "./SortableBlockContext";
import type { SortableBlockListeners } from "./sortableBlockContextValue";

/**
 * wrap an AppNode component so every instance registers itself
 * with dnd-kit via `useSortable({ id })`. Without this call, the scope-keyed
 * `SortableContext` knows the ordered ids and the sensors are configured,
 * but no individual block is a draggable item — so PointerSensor has nothing
 * to pick up when the user presses on the grip handle.
 *
 * The HOC is intentionally a transparent wrapper: it does not apply the
 * `transform` / `transition` returned by `useSortable` to the node root,
 * because React Flow already controls the absolute position of every
 * `.react-flow__node` wrapper. The `DragOverlay` portal (see FlowRenderer)
 * renders the drag ghost instead, and the original node fades to signal
 * which block is being moved.
 *
 * `listeners` + `isDragging` flow to `NodeGripHandle` through
 * `SortableBlockContext` so node components keep their existing signatures.
 */
function withSortableBlock<P extends NodeProps>(
  Component: ComponentType<P>,
): ComponentType<P> {
  function SortableBlock(props: P) {
    const { setNodeRef, attributes, listeners, isDragging } = useSortable({
      id: props.id,
    });

    // The wrapper div exists so `setNodeRef` can target an element React
    // Flow doesn't control — RF owns positioning on the outer
    // `.react-flow__node` and dnd-kit needs a stable inner ref for
    // measurement. We deliberately do NOT spread `attributes` on the
    // wrapper itself: `useSortable` would put `role="button"` + `tabIndex`
    // on a div that wraps a node containing its own grip-handle button,
    // creating a duplicate focusable + a non-button role. Instead we
    // forward the screen-reader-relevant attrs (`aria-describedby` /
    // `aria-roledescription`) via context so `NodeGripHandle` can spread
    // them onto the real focusable button — that way VoiceOver/NVDA pick
    // up the `SCREEN_READER_INSTRUCTIONS` hidden `<p>` that DndContext
    // renders, and announce the sortable role on focus.
    const {
      "aria-describedby": ariaDescribedBy,
      "aria-roledescription": ariaRoleDescription,
    } = attributes as {
      "aria-describedby"?: string;
      "aria-roledescription"?: string;
    };
    return (
      <SortableBlockProvider
        listeners={listeners as SortableBlockListeners}
        attributes={{
          "aria-describedby": ariaDescribedBy,
          "aria-roledescription": ariaRoleDescription,
        }}
        isDragging={isDragging}
      >
        <div
          ref={setNodeRef}
          data-sortable-id={props.id}
          style={{ opacity: isDragging ? 0.4 : undefined }}
        >
          <Component {...props} />
        </div>
      </SortableBlockProvider>
    );
  }
  SortableBlock.displayName = `withSortableBlock(${Component.displayName ?? Component.name ?? "Component"})`;
  return SortableBlock;
}

export { withSortableBlock };
