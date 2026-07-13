import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";

/**
 * Segmented-control button shared by the studio's view toggles (pane headers).
 * Collapses to its icon when the host header is compact. Labelled states carry
 * no tooltip (only icon-only controls tooltip); compact moves the label into
 * one, and an explicit `title` (status info) always shows.
 */
export function ViewToggle({
  active,
  onClick,
  icon,
  label,
  compact,
  title,
  ariaLabel,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  compact: boolean;
  title?: string;
  // Overrides the accessible name without changing the visible label, e.g. to
  // append a badge's state ("Outputs, new output") to a screen reader only.
  ariaLabel?: string;
}) {
  const tip = title ?? (compact ? (ariaLabel ?? label) : undefined);
  const button = (
    <button
      type="button"
      onClick={onClick}
      aria-label={ariaLabel ?? label}
      aria-pressed={active}
      className={cn(
        "inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-[11px] font-medium",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        active
          ? "bg-accent text-foreground"
          : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
      )}
    >
      {icon}
      {compact ? null : label}
    </button>
  );
  if (!tip) {
    return button;
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>{button}</TooltipTrigger>
      <TooltipContent side="bottom">{tip}</TooltipContent>
    </Tooltip>
  );
}
