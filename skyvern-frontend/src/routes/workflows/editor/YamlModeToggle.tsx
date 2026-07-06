import { CodeIcon, EyeOpenIcon } from "@radix-ui/react-icons";

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";

const segmentClass = (active: boolean) =>
  cn(
    "flex h-7 items-center gap-1 rounded-md px-2 text-xs transition-colors",
    "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
    active
      ? "bg-accent text-foreground"
      : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
  );

type Props = {
  mode: "visual" | "code";
  onVisual?: () => void;
  onCode?: () => void;
  disabled?: boolean;
  // Icon-only segments (narrow pane headers); labels move to tooltips.
  compact?: boolean;
};

// Segmented Visual/Code toggle for the workflow editor. "Code" opens the
// full-screen YAML editor; "Visual" commits the YAML back into the canvas.
export function YamlModeToggle({
  mode,
  onVisual,
  onCode,
  disabled,
  compact = false,
}: Props) {
  const segment = (
    label: "Visual" | "Code",
    icon: React.ReactNode,
    onClick?: () => void,
  ) => {
    const button = (
      <button
        type="button"
        aria-pressed={mode === label.toLowerCase()}
        aria-disabled={mode === label.toLowerCase() || undefined}
        aria-label={label}
        disabled={disabled}
        onClick={mode === label.toLowerCase() ? undefined : onClick}
        className={segmentClass(mode === label.toLowerCase())}
      >
        {icon}
        {compact ? null : label}
      </button>
    );
    if (!compact || disabled) {
      return button;
    }
    return (
      <Tooltip>
        <TooltipTrigger asChild>{button}</TooltipTrigger>
        <TooltipContent side="bottom">{label}</TooltipContent>
      </Tooltip>
    );
  };

  return (
    <div className="flex items-center gap-1">
      {/* The active segment stays focusable (aria-disabled, not disabled) so
          keyboard users can tab to it and hear the pressed state. */}
      {segment("Visual", <EyeOpenIcon className="size-3" />, onVisual)}
      {segment("Code", <CodeIcon className="size-3" />, onCode)}
    </div>
  );
}
