import {
  useRef,
  type AnimationEvent,
  type CSSProperties,
  type ReactNode,
} from "react";

import { CollapsibleContent } from "@/components/ui/collapsible";
import { cn } from "@/util/utils";

type Props = {
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
};

// Chrome compositing layer staleness: after the `collapsible-down` keyframe
// ends, the body's content can render into a parent compositing layer that
// is promoted by Flippable's `transform-style: preserve-3d` plus the outer
// block's `transition-all`. The painted snapshot is stale until any
// subsequent interaction forces a recomposite.
function forceRecomposite(el: HTMLElement) {
  const originalTransform = el.style.transform;
  el.style.transform = originalTransform
    ? `${originalTransform} translateZ(0)`
    : "translateZ(0)";
  // Force the browser to flush the style change before reverting.
  void el.offsetHeight;
  el.style.transform = originalTransform;
}

export function NodeBody({ children, className, style }: Props) {
  const recompositeRef = useRef<HTMLDivElement>(null);

  const onAnimationEnd = (event: AnimationEvent<HTMLDivElement>) => {
    const isOwnCollapse =
      event.animationName === "collapsible-down" &&
      event.target === event.currentTarget;
    const isNestedAccordion = event.animationName === "accordion-down";
    if (!isOwnCollapse && !isNestedAccordion) {
      return;
    }

    if (recompositeRef.current) {
      forceRecomposite(recompositeRef.current);
    }
  };

  return (
    <CollapsibleContent
      onAnimationEnd={onAnimationEnd}
      style={style}
      className={cn(
        "overflow-hidden",
        // Collapse the wrapper to nothing when children render to null
        // (e.g., a leaf block in /edit mode whose form is gated to /build);
        // otherwise the outer `space-y-4` still reserves a 16px gap below
        // the header for an empty body.
        "empty:hidden",
        "[&:has(>[data-node-body-recomposite]:empty)]:hidden",
        "data-[state=open]:animate-collapsible-down",
        "data-[state=closed]:animate-collapsible-up",
        "motion-reduce:animate-none",
        className,
      )}
    >
      {/* p-1 keeps focused-input rings/shadows off the overflow-hidden clip
          edge so they don't render clipped (SKY-10457). Padding lives on this
          inner wrapper, not the height-animated CollapsibleContent, so the
          collapsed state still measures to zero. */}
      <div ref={recompositeRef} data-node-body-recomposite="" className="p-1">
        {children}
      </div>
    </CollapsibleContent>
  );
}
