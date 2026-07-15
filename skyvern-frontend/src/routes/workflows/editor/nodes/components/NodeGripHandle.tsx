import { forwardRef } from "react";

import { cn } from "@/util/utils";
import { useSortableBlockContext } from "../../sortable/useSortableBlockContext";

interface NodeGripHandleProps extends Omit<
  React.ButtonHTMLAttributes<HTMLButtonElement>,
  "aria-grabbed" | "role"
> {
  isDragging?: boolean;
  disabled?: boolean;
  blockLabel?: string;
}

const NodeGripHandle = forwardRef<HTMLButtonElement, NodeGripHandleProps>(
  function NodeGripHandle(
    {
      isDragging = false,
      disabled = false,
      className,
      blockLabel,
      "aria-label": ariaLabelOverride,
      onPointerDown: onPointerDownProp,
      ...buttonProps
    },
    ref,
  ) {
    const accessibleName =
      ariaLabelOverride ??
      (blockLabel && blockLabel.length > 0
        ? `Drag to reorder block ${blockLabel}`
        : "Drag to reorder block");
    // Drag listeners + sortable-aria attrs come from a context populated
    // by withSortableBlock rather than being prop-drilled. When mounted
    // outside the provider (most unit tests) both are undefined and the
    // wiring no-ops.
    const {
      listeners,
      attributes,
      isDragging: contextIsDragging,
    } = useSortableBlockContext();
    const effectiveIsDragging = isDragging || contextIsDragging;
    const dragListeners = disabled ? undefined : listeners;
    const dragPointerDown = dragListeners?.onPointerDown as
      | ((event: React.PointerEvent<HTMLButtonElement>) => void)
      | undefined;
    return (
      <button
        ref={ref}
        type="button"
        aria-label={accessibleName}
        aria-describedby={attributes?.["aria-describedby"]}
        aria-roledescription={attributes?.["aria-roledescription"]}
        aria-disabled={disabled || undefined}
        aria-keyshortcuts="Space"
        data-dragging={effectiveIsDragging ? "true" : undefined}
        data-disabled={disabled ? "true" : undefined}
        disabled={disabled}
        className={cn(
          // `nodrag nopan` are React Flow's opt-out classes. Without them,
          // RF's pane listener (panOnDrag={true}) consumes the pointerdown
          // before dnd-kit's PointerSensor can activate. Every other
          // interactive element inside an RF node in this codebase carries
          // these classes — the grip handle shipped without them.
          "nodrag nopan flex h-[2.75rem] w-5 shrink-0 cursor-grab items-center justify-center rounded text-muted-foreground opacity-0 transition-[opacity,colors] hover:bg-muted hover:text-tertiary-foreground focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring active:cursor-grabbing group-hover:opacity-100 data-[dragging=true]:opacity-100 dark:text-slate-500",
          effectiveIsDragging &&
            "cursor-grabbing text-foreground dark:text-slate-200",
          disabled &&
            "pointer-events-none cursor-not-allowed text-slate-700 opacity-50 hover:bg-transparent hover:text-slate-700 active:cursor-not-allowed",
          className,
        )}
        {...dragListeners}
        {...buttonProps}
        onPointerDown={(event) => {
          // Belt-and-suspenders with `nodrag nopan` on the className: stop
          // propagation so React Flow's pane / selection handlers cannot
          // claim the pointerdown before dnd-kit's PointerSensor sees it,
          // then forward to dnd-kit's listener (if present) and any
          // caller-supplied handler so drag activation still fires.
          event.stopPropagation();
          dragPointerDown?.(event);
          onPointerDownProp?.(event);
        }}
      >
        <svg
          width="8"
          height="14"
          viewBox="0 0 8 14"
          fill="currentColor"
          aria-hidden="true"
          focusable="false"
        >
          <circle cx="1.5" cy="1.5" r="1.5" />
          <circle cx="6.5" cy="1.5" r="1.5" />
          <circle cx="1.5" cy="7" r="1.5" />
          <circle cx="6.5" cy="7" r="1.5" />
          <circle cx="1.5" cy="12.5" r="1.5" />
          <circle cx="6.5" cy="12.5" r="1.5" />
        </svg>
      </button>
    );
  },
);

export { NodeGripHandle };
export type { NodeGripHandleProps };
