import type { NodeProps } from "@xyflow/react";
import type { ComponentType } from "react";

import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { cn } from "@/util/utils";

import { SELECTED_RING_CLASSES } from "./selectedRingClasses";

/**
 * wrap an AppNode component so its rendered card receives the
 * canvas-selected visual when `selectedBlockId` in `WorkflowPanelStore`
 * matches this node's id. Centralizing here means all 26 block types pick
 * up the selected state uniformly without each component re-implementing
 * the read-from-store + class merge.
 *
 * Composed *outside* `withCollapsible` so collapsed and expanded shells
 * both reflect the selected ring, and *inside* `withSortableBlock` so the
 * dnd-kit measurement node remains the outermost wrapper.
 */
function withSelectableBlock<P extends NodeProps>(
  Component: ComponentType<P>,
): ComponentType<P> {
  function SelectableBlock(props: P) {
    const isSelected = useWorkflowPanelStore(
      (state) => state.selectedBlockId === props.id,
    );

    return (
      <div
        data-selected={isSelected ? "true" : undefined}
        data-selectable-id={props.id}
        className={cn(
          "rounded-lg transition-shadow",
          isSelected && SELECTED_RING_CLASSES,
        )}
      >
        <Component {...props} />
      </div>
    );
  }
  SelectableBlock.displayName = `withSelectableBlock(${Component.displayName ?? Component.name ?? "Component"})`;
  return SelectableBlock;
}

export { withSelectableBlock };
