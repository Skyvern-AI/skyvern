import { cn } from "@/util/utils";

/**
 * Segmented-control button shared by the studio's view toggles (run hero,
 * browser pane header). Collapses to its icon when the host header is compact.
 */
export function ViewToggle({
  active,
  onClick,
  icon,
  label,
  compact,
  title,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  compact: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title ?? (compact ? label : undefined)}
      aria-label={label}
      aria-pressed={active}
      className={cn(
        "inline-flex items-center gap-1.5 rounded px-2 py-1 text-[11px] font-medium",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        active
          ? "bg-studio-accent/15 text-foreground"
          : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
      )}
    >
      {icon}
      {compact ? null : label}
    </button>
  );
}
