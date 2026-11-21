import * as React from "react";

import { cn } from "@/util/utils";

type MenuItemComponent = React.ComponentType<
  Pick<
    React.AriaAttributes,
    "aria-disabled" | "aria-label" | "aria-describedby"
  > & {
    className?: string;
    onSelect?: (event: Event) => void;
    children?: React.ReactNode;
  }
>;

// Radix skips a `disabled` item during keyboard navigation, so the item stays focusable
// and refuses selection instead; that way keyboard and screen-reader users reach the reason.
function DisabledReasonMenuItem({
  item: Item,
  label,
  icon,
  reason,
  className,
}: {
  item: MenuItemComponent;
  label: React.ReactNode;
  icon?: React.ReactNode;
  reason: string;
  className?: string;
}) {
  const reasonId = React.useId();
  return (
    <Item
      aria-disabled
      // The name stays the action; the visible reason line is its description.
      aria-label={typeof label === "string" ? label : undefined}
      aria-describedby={reasonId}
      className={cn("flex-col items-start gap-1", className)}
      onSelect={(event) => event.preventDefault()}
    >
      <span className="flex items-center opacity-50">
        {icon}
        {label}
      </span>
      <span
        id={reasonId}
        className="max-w-xs whitespace-normal text-xs text-muted-foreground"
      >
        {reason}
      </span>
    </Item>
  );
}

export { DisabledReasonMenuItem };
